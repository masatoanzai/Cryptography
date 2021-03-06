#Created by Masato Anzai
#imported TORNADO.io
#imported PYCRYPTO
import os
import signal
import argparse
import ssl
import socket
import logging

from tornado.ioloop import IOLoop
from tornado.iostream import SSLIOStream
from tornado.tcpserver import TCPServer

from Crypto import Random
from Crypto.PublicKey import RSA
from Crypto.Hash import MD5


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

CRLF = '\r\n'

CLIENT_OP = 1
SERVER_OP = 2

OP_PUBKEY = 1
OP_MESSAGE = 2

KEYS_PATH = 'priv/'
SERVER_CRT_PATH = 'priv/server/server.crt'
SERVER_KEY_PATH = 'priv/server/server.key'
CLIENT_PUB_PATH = KEYS_PATH + '/clients/%s.pub'
CLIENT_PRIV_PATH = KEYS_PATH + '/clients/%s.priv'

clients = dict()
offline_messages = dict()

def handle_signal(sig, frame):
    IOLoop.instance().add_callback(IOLoop.instance().stop)

class Protocol(object):

    commands = {
        OP_PUBKEY:  (SERVER_OP, 3, 'handle_op_pubkey'),
        OP_MESSAGE: (CLIENT_OP, 5, 'handle_op_message'),
    }

    def __init__(self, role):
        assert role in ['client', 'server']
        self.role = role
        self.opcode = None
        self.buffer = ''
        self.pubkey = None

    def handle_line(self, data):
        self.buffer += data
        messages = self.buffer.split(CRLF)

        if not self.opcode and len(messages) > 1:
            self.opcode = int(messages[0])

        if self.opcode:
            opmeta = self.commands[self.opcode]
            if len(messages) == opmeta[1]:
                message = messages[1:opmeta[1]-1]

                name = opmeta[2]
                handler = getattr(self, name)
                handler(*message)

                self.buffer = ''
                self.opcode = None

        self.read_line()

    def handle_op_message(self, *args, **kwargs):
        if self.role == 'client':
            self.handle_client_op_message(*args, **kwargs)
        elif self.role == 'server':
            self.handle_server_op_message(*args, **kwargs)

    def handle_client_op_message(self, pubkey, enc, sig):
        enc = (enc,)
        sig = (long(sig),)
        if self.verify_signature(pubkey, sig, enc[0]):
            message = self.decrypt_message(enc)
            logger.info('rcvd %s from %s' % (message, pubkey))

    def handle_op_pubkey(self, pubkey):
        self.pubkey = pubkey
        clients[self.pubkey] = self.conn
        logger.debug('Pubkey %s connected' % self.pubkey)

        if self.pubkey in offline_messages:
            messages = offline_messages[self.pubkey]
            del offline_messages[self.pubkey]
            logger.debug('Sending %s offline messages found for pubkey %s' % (len(messages), self.pubkey))
            for message in messages:
                self.write(self.conn, *message)

    def handle_server_op_message(self, pubkey, enc, sig):
        message = (OP_MESSAGE, self.pubkey, enc, sig,)
        if pubkey in clients:
            conn = clients[pubkey]
            self.write(conn, *message)
            logger.debug('sent message to pubkey %s' % pubkey)
        else:
            if pubkey not in offline_messages:
                offline_messages[pubkey] = list()
            offline_messages[pubkey].append(message,)
            logger.debug('pubkey %s is offline, message cached' % pubkey)

class Client(Protocol):

    def __init__(self, uid, port):
        super(Client, self).__init__('client')

        self.uid = uid
        self.port = port

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn = SSLIOStream(self.sock, ssl_options={'ca_certs':SERVER_CRT_PATH, 'cert_reqs':ssl.CERT_REQUIRED})
        self.conn.connect(('127.0.0.1', self.port), self.on_connect)

    def on_connect(self):
        self.conn.set_close_callback(self.on_close)
        self.pubkey, self.privkey = None, None
        self.init_keys()
        self.write(OP_PUBKEY, self.pubkey)
        if self.uid == 2:
            self.send_message(1, 'Applied Cryptography')
            logger.debug('Client %s: Sending message "Applied Cryptography" to user with uid 1' % self.uid)
        self.read_line()

    def read_line(self):
        self.conn.read_until(CRLF, self.handle_line)

    def on_close(self):
        self.conn = None
        self.sock = None

    def init_keys(self):
        if os.path.isfile(CLIENT_PUB_PATH % self.uid) and os.path.isfile(CLIENT_PRIV_PATH % self.uid):
            with open(CLIENT_PUB_PATH % self.uid, 'rb') as pubfile, open(CLIENT_PRIV_PATH % self.uid, 'rb') as privfile:
                self.pubkey = pubfile.read().strip()
                self.privkey = privfile.read().strip()
                logger.debug('read existing pub/priv key for uid %s' % self.uid)
        else:
            self.pubkey, self.privkey = self.generate_keys()
            with open(CLIENT_PUB_PATH % self.uid, 'wb') as pubfile, open(CLIENT_PRIV_PATH % self.uid, 'wb') as privfile:
                pubfile.write(self.pubkey)
                privfile.write(self.privkey)
                logger.debug('written pub/priv key for uid %s' % self.uid)

    def write(self, *messages):
        for message in messages:
            self.conn.write('%s%s' % (message, CRLF))

    def send_message(self, uid, message):
        with open(CLIENT_PUB_PATH % uid, 'rb') as pubfile:
            pubkey = pubfile.read()
            # encrypt message using receiver public key
            enc = self.encrypt_message(message, pubkey)
            # sign encrypted message for digital verification
            sig = self.generate_signature(enc[0])
            message = (OP_MESSAGE, pubkey, enc[0], sig[0],)
            self.write(*message)

    @staticmethod
    def generate_keys():
        random_generator = Random.new().read
        priv = RSA.generate(1024, random_generator)
        pub = priv.publickey()
        return (pub.exportKey().strip(), priv.exportKey().strip())

    def generate_signature(self, message):
        '''sign messaging using our priv key'''
        k = RSA.importKey(self.privkey)
        h = MD5.new(message).digest()
        return k.sign(h, '')

    @staticmethod
    def verify_signature(pubkey, signature, message):
        '''verify signature using signing user public key'''
        k = RSA.importKey(pubkey)
        h = MD5.new(message).digest()
        return k.verify(h, signature)

    @staticmethod
    def encrypt_message(message, pubkey):
        '''encrypt message using receiving user public key'''
        k = RSA.importKey(pubkey)
        return k.encrypt(message, 32)

    def decrypt_message(self, enc):
        '''decrypt message using our priv key'''
        k = RSA.importKey(self.privkey)
        return k.decrypt(enc)

class Handler(Protocol):
    '''client connection handler.'''

    def __init__(self, conn, addr):
        super(Handler, self).__init__('server')
        self.conn = conn
        self.addr = addr

    def run(self):
        self.read_line()

    def read_line(self):
        self.conn.read_until(CRLF, self.handle_line)

    @staticmethod
    def write(conn, *messages):
        for message in messages:
            conn.write('%s%s' % (message, CRLF))

class Server(TCPServer):

    def __init__(self, port):
        self.port = port
        super(Server, self).__init__(ssl_options={'certfile':SERVER_CRT_PATH, 'keyfile':SERVER_KEY_PATH})
        logger.info('listening on port %s' % self.port)
        self.listen(self.port)

    def handle_stream(self, conn, addr):
        handler = Handler(conn, addr)
        handler.run()

def start_client(opts):
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    uid = int(opts.uid)
    _c = Client(uid, opts.port)
    IOLoop.instance().start()
    IOLoop.instance().close()

def start_server(opts):
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    _s = Server(opts.port)
    IOLoop.instance().start()
    IOLoop.instance().close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True, help='server or client')
    parser.add_argument('--uid', help='client id, required for local testing purposes only')
    parser.add_argument('--port', default=11111, type=int, help='server port')
    opts = parser.parse_args()

    component = opts.start.lower()
    if component == 'client' and not opts.uid:
        parser.error('uid required for client')

    start = start_client if component == 'client' else start_server
    start(opts)
