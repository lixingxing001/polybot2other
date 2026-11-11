"""只用人工 NTP 数据包；测试不触网，也不设置系统时钟。"""
import struct
import unittest
from polybot2other.hengzhun.clock import _encode_timestamp,parse_response,probe,ClockMonitor


class NtpPacketTests(unittest.TestCase):
    def setUp(self):
        self.t=1_800_000_000.
        self.request=_encode_timestamp(self.t)
        self.packet=bytearray(48);self.packet[0]=0x24;self.packet[1]=2;self.packet[3]=236
        self.packet[4:8]=struct.pack('!i',int(.02*65536))
        self.packet[8:12]=struct.pack('!I',int(.01*65536))
        self.packet[16:24]=_encode_timestamp(self.t-10)
        self.packet[24:32]=self.request
        self.packet[32:40]=_encode_timestamp(self.t+.06)
        self.packet[40:48]=_encode_timestamp(self.t+.08)

    def parsed(self,packet=None,**changes):
        args={'sent_at':self.t,'received_at':self.t+.1,'monotonic_rtt':.1,'server':'test.ntp','address':('192.0.2.1',123)}
        args.update(changes)
        return parse_response(bytes(packet or self.packet),self.request,**args)

    def test_offset_bound_includes_roundtrip_root_delay_dispersion_and_precision(self):
        result=self.parsed()
        self.assertAlmostEqual(result['offset_ms'],20,places=3)
        self.assertGreater(result['offset_bound_ms'],90)
        self.assertLess(result['offset_bound_ms'],92)
        self.assertEqual(result['stratum'],2)
        self.assertFalse(result['authenticated']);self.assertFalse(result['system_clock_modified'])

    def test_unsynchronized_kod_bad_modes_and_wrong_origin_are_rejected(self):
        for index,value in [(0,0xe4),(0,0x23),(0,0x14),(1,0),(1,16)]:
            with self.subTest(index=index,value=value):
                packet=bytearray(self.packet);packet[index]=value
                with self.assertRaises(ValueError):self.parsed(packet)
        packet=bytearray(self.packet);packet[24:32]=_encode_timestamp(self.t-1)
        with self.assertRaisesRegex(ValueError,'ORIGIN'):self.parsed(packet)

    def test_truncated_zero_reversed_timestamps_and_probe_clock_jump_rejected(self):
        with self.assertRaisesRegex(ValueError,'TRUNCATED'):self.parsed(self.packet[:30])
        for target in (slice(16,24),slice(32,40),slice(40,48)):
            packet=bytearray(self.packet);packet[target]=bytes(8)
            with self.assertRaisesRegex(ValueError,'ZERO'):self.parsed(packet)
        packet=bytearray(self.packet);packet[40:48]=_encode_timestamp(self.t+.01)
        with self.assertRaisesRegex(ValueError,'ORDER'):self.parsed(packet)
        with self.assertRaisesRegex(ValueError,'CLOCK_JUMP'):self.parsed(received_at=self.t+1)

    def test_read_only_probe_sends_client_packet_and_retains_server_evidence(self):
        fixture=self
        class FakeSocket:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def settimeout(self,timeout):fixture.assertEqual(timeout,2)
            def connect(self,address):fixture.assertEqual(address,('192.0.2.1',123))
            def send(self,packet):fixture.assertEqual(packet[0],0x23);fixture.assertEqual(packet[40:48],fixture.request)
            def recv(self,size):return bytes(fixture.packet)
        wall=iter([self.t,self.t+.1]);mono=iter([10.,10.1])
        result=probe('test.ntp',wall_clock=lambda:next(wall),monotonic=lambda:next(mono),
                     socket_factory=lambda *a:FakeSocket(),resolver=lambda *a,**k:[(2,2,17,'',('192.0.2.1',123))])
        self.assertEqual(result['status'],'VERIFIED');self.assertEqual(result['server'],'test.ntp')
        self.assertEqual(len(result['packet_sha256']),64)


class ClockMonitorTests(unittest.TestCase):
    def setUp(self):
        self.wall=1800000000.;self.mono=10.
        self.monitor=ClockMonitor(wall_clock=lambda:self.wall,monotonic=lambda:self.mono,
            probe_fn=lambda **k:{'status':'VERIFIED','checked_at':int(self.wall*1000),'offset_bound_ms':50.,'server':'test.ntp'})

    def test_no_evidence_is_unknown_not_zero_and_stale_evidence_cannot_authorize(self):
        status=self.monitor.snapshot()
        self.assertEqual(status['status'],'CLOCK_UNVERIFIED');self.assertIsNone(status['offset_bound_ms'])
        self.monitor.probe();self.wall+=121;self.mono+=121
        status=self.monitor.snapshot()
        self.assertEqual(status['status'],'CLOCK_UNVERIFIED');self.assertEqual(status['reason'],'CLOCK_EVIDENCE_STALE')

    def test_bound_grows_and_excess_offset_blocks_only_this_monitor(self):
        self.monitor.probe();self.wall+=60;self.mono+=60
        self.assertAlmostEqual(self.monitor.snapshot()['offset_bound_ms'],50.9,places=4)
        self.monitor.probe_fn=lambda **k:{'status':'VERIFIED','checked_at':int(self.wall*1000),'offset_bound_ms':300.}
        self.assertEqual(self.monitor.probe()['status'],'CLOCK_OFFSET_EXCEEDED')

    def test_forward_and_backward_wall_clock_changes_invalidate_proof(self):
        for jump in (1.,-1.):
            with self.subTest(jump=jump):
                self.monitor.probe();self.wall+=10+jump;self.mono+=10
                status=self.monitor.snapshot()
                self.assertEqual(status['status'],'CLOCK_UNVERIFIED')
                self.assertEqual(status['reason'],'CLOCK_JUMP_DETECTED')
                self.assertAlmostEqual(status['last_jump']['delta_ms'],jump*1000,places=3)

    def test_network_error_clears_old_proof_without_adjusting_clock(self):
        self.monitor.probe()
        def fail(**k):raise TimeoutError('private transport details')
        self.monitor.probe_fn=fail
        result=self.monitor.probe()
        self.assertEqual(result['status'],'CLOCK_UNVERIFIED');self.assertEqual(result['reason'],'TimeoutError')
        self.assertNotIn('private',str(result));self.assertEqual(self.wall,1800000000.)

    def test_millisecond_quantization_does_not_accumulate_false_drift(self):
        self.monitor.probe()
        for i in range(240):
            self.wall+=.2504;self.mono+=.2504;status=self.monitor.snapshot(int(self.wall*1000))
        self.assertEqual(status['status'],'VERIFIED');self.assertLess(status['offset_bound_ms'],53)


if __name__=='__main__':unittest.main()
