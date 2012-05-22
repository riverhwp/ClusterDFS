import io
import os
import os.path
import errno
import socket
import logging
import tempfile
import argparse

from coding import *
from headers import *
from networking import *
from bufferedio import *

class DataNodeConfig(object):
    port = 7777
    bind_addr = '0.0.0.0'
    datadir = tempfile.mkdtemp()
    namenode_addr = 'localhost'
    namenode_port = 7770
    ping_timeout = 10
    isolated = False

    def __init__(self, args):
        for k, v in args.__dict__.iteritems():
            if v!=None: self.__dict__[k] = v

        if not self.datadir.endswith('/'):
            self.datadir = self.datadir+'/'

class BlockStoreManager(object):
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def path(self, block_id):
        return os.path.join(self.data_dir, block_id)

    def get_size(self, block_id):
        return os.path.getsize(self.path(block_id))

    def get_input_stream(self, block_id):
        '''
            Returns a FileInputStream for the block with block_id.
        '''
        return FileInputStream(self.path(block_id))

    def get_output_stream(self, block_id):
        '''
            Returns a FileOutputStream for the block with block_id.
        '''
        return FileOutputStream(self.path(block_id))


@ClassLogger
class DataNodeQuery(ServerHandle):
    def process_query(self):
        if self.header['op']==DataNodeHeader.OP_STORE:
            return self.store_block()
        
        elif self.header['op']==DataNodeHeader.OP_RETRIEVE:
            return self.retrieve_block()
        
        elif self.header['op']==DataNodeHeader.OP_CODING:
            return self.node_coding()
        
        else:
            assert False
    
    def store_block(self):
        # Read block properties
        block_id = self.header['id']
        block_size = self.header['length']
        logging.info("Receiving block '%s' (%d bytes) from %s.", block_id, block_size, self.address)

        # Check headers
        if block_size<=0:
            return NetworkHeader.error(msg='Block size has to be larger than zero.')

        # Get the forward list and the next forward node
        if 'fwdlist' in self.header:
            forward_list = self.header['fwdlist']
            logging.info("Forwarding '%s' to %s.", block_id, repr(forward_list[0]))
            logging.info("Remaining forwards: %d.", len(forward_list)-1)
            next_node = Client(*forward_list[0])
            next_forward_list = forward_list[1:]
        else:
            next_node = None
            next_forward_list = []

        block_input_stream = None
        local_block_output_stream = None

        try:
            # prepare streams...
            block_input_stream = SocketInputStream(self.socket, block_size)
            local_block_output_stream = self.server.block_store.get_output_stream(block_id)

            if next_node:
                # Send header to next node
                header = self.header.copy()
                header['fwdlist'] = next_forward_list
                next_node.send(header)
            
                # Prepare stream
                next_node_output_stream = SocketOutputStream(next_node.socket)
                
                # store and send to next node
                reader = InputStreamReader(block_input_data)
                writer = OutputStreamWriter(local_block_output_stream, next_node_output_stream)
                reader.read_into(writer)
            
                # Receive response from next_node
                response = next_node.recv()
                if response['code']==NetworkHeader.OK:
                    logging.info("Block '%s' (%d bytes) stored & forwarded successfully."%(block_id, block_size))
                    return NetworkHeader.ok(msg='Block stored & forwarded successfully.')
                else:
                    return response

            else:
                # store only
                reader = InputStreamReader(block_input_data)
                writer = OutputStreamWriter(local_block_output_stream)
                reader.read_into(writer)
            
                logging.info("Block '%s' (%d bytes) stored successfully."%(block_id, block_size))
                return NetworkHeader.ok(msg='Block stored successfully.')

        except IOError:
            logging.info("Transmission from %s failed.", self.address)
            return NetworkHeader.error(msg='Transmission failed.')

        finally:
            # Release sockets and close files
            if next_node:
                next_node.kill() # there is no need to close output_stream since endpoint does it.
            if local_block_output_stream:
                local_block_output_stream.close()

    def retrieve_block(self):
        # Read block properties
        block_id = self.header['id']
        block_size = self.server.block_store.get_size(block_id)
        block_offset = self.header['offset'] if ('offset' in self.header) else 0
        block_length = self.header['length'] if ('length' in self.header) else block_size

        # Do error control
        if block_length+block_offset > block_size:
            return NetworkHeader.error(msg='The requested data is larger than block_size.')

        self.logger.info("Sending block '%s' (%d bytes, %d offset) to %s."%(block_id, block_length, block_offset, self.address))

        try:
            # Send response
            self.send_response(NetworkHeader.response(length=block_length))
            
            # Send block data
            block_finput_stream = self.server.block_store.get_input_stream(block_id)
            local_block_output_stream = SocketOutputStream(self.socket)
            reader = InputStreamReader(block_finput_stream)
            writer = OutputStreamWriter(local_block_output_stream)
            reader.read_into(writer)

        except IOError:
            self.logger.error("Transmission from %s failed.", self.address)
            return NetworkHeader.error(msg='Transmission failed.')
        
        finally:
            # there is no need to close output_stream since endpoint does it.
            block_finput_stream.close()

    def node_coding(self):
        self.logger.debug("Starting coding operation.")

        coding_operations = NetCodingOperations.unserialize(self.header['coding'])
        coding_executor = NetCodingExecutor(coding_operations, self.server.block_store)
    
        if coding_operations.is_stream():
            self.logger.debug("Forwarding coding stream.")
            input_stream = NetCodingInputStream(coding_executor)
            reader = InputStreamReader(input_stream, debug_name='coding_result')
            self.send(input_stream)
            for iobuffer in reader:
                if __debug__: self.logger.debug('Sending coded buffer.')
                writer = self.new_writer()
                writer.write(iobuffer)
                if __debug__: self.logger.debug('Waiting for writer to finalize.')
                writer.join() 
        else:
            if __debug__: self.logger.debug("Executing locally.")
            coding_executor.execute()

        self.logger.debug('Coding finalized successfully.')

@ClassLogger
class DataNodeNotifier(object):
    def __init__(self, config, server):
        self.config = config
        self.server = server
        #self.process = gevent.spawn(self.timeout)
        self.ping = {'op':NameNodeHeader.OP_PING, 'datanode_port':self.config.port}

    def stop(self):
        self.process.kill()

    def timeout(self):
        while True:
            # send ping
            try:
                logging.debug('Sending ping.')
                ne = NetworkEndpoint(gevent.socket.create_connection((self.config.namenode_addr, self.config.namenode_port)))
                ne.send(self.ping)
                ne.send([])
                response = ne.recv()
                if response['code']!=NetworkHeader.OK:
                    logging.error('Cannot deliver ping to nameserver: %s', response['msg']) 

            except socket.error, (value,message):
                logging.error("Cannot deliver ping to nameserver: %s."%(message))
            
            # sleep timeout
            gevent.sleep(self.config.ping_timeout)

class DataNode(Server):
    def __init__(self, config):
        self.config = config
        logging.info("Configuring DataNode to listen on localhost:%d"%(self.config.port))
        logging.info("DataNode data dir: %s"%(config.datadir))
        Server.__init__(self, DataNodeQuery, port=self.config.port)

        self.block_store = BlockStoreManager(self.config.datadir)

        if not self.config.isolated:
            self.notifier = DataNodeNotifier(self.config, self)

    def init(self):
        self.serve()

    def finalize(self):
        if not self.config.isolated:
            self.notifier.stop()
