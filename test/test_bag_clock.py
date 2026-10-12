"""Recorded ROS clocks must affect age, never arrival windows or budgets."""
import struct
import sys
import types
from types import SimpleNamespace as NS

import pytest

from ros_graph_debugger.assertions import evaluate
from ros_graph_debugger.bag_analyze import BagAnalyzer, analyze_bag, stamp_from_cdr
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.recording import read_recording

T0 = 1_700_000_000.0
POSE = {'/pose': 'geometry_msgs/msg/PoseStamped'}


def cdr_time(sec, nano=0, endian='<'):
    return (b'\x00\x01\x00\x00' if endian == '<' else b'\x00\x00\x00\x00') + struct.pack(endian+'iI',sec,nano)


@pytest.mark.parametrize('endian', ['<', '>'])
def test_nonzero_subsecond_header_is_valid(endian):
    assert stamp_from_cdr(cdr_time(0,500_000_000,endian)) == 0.5
    assert stamp_from_cdr(cdr_time(0,0,endian)) is None
    assert stamp_from_cdr(cdr_time(-1,0,endian)) is None
    assert stamp_from_cdr(cdr_time(1,1_000_000_000,endian)) is None


def sample_clock(age_clock, lag):
    th = Thresholds(expected_max_age_ms={'/pose':100})
    analyzer = BagAnalyzer(POSE,th,age_clock=age_clock)
    for i in range(100):
        elapsed = i/50
        analyzer.update_ros_clock(100+elapsed)
        analyzer.add_message('/pose',T0+elapsed,100,stamp=100+elapsed-lag)
    return analyzer.snapshots(interval=0.5,window=1)


def test_recorded_clock_removes_calendar_offset_without_changing_arrivals():
    ros = sample_clock('ros',0.02)
    bag = sample_clock('bag',0.02)
    for fixed,old in zip(ros,bag):
        assert fixed['timestamp'] == old['timestamp']
        a,b = fixed['topics'][0],old['topics'][0]
        assert a['rate_hz'] == b['rate_hz'] == 50
        assert a['bandwidth_bps'] == b['bandwidth_bps'] == 5000
        assert a['last_seen_time'] == b['last_seen_time']
        assert a['header_age_p95_ms'] == 20
        assert b['header_age_p95_ms'] > 1_000_000_000
    assert evaluate(ros,fail_on='never',forbid_kinds=['stale_data'])['passed']
    assert not evaluate(bag,fail_on='never',forbid_kinds=['stale_data'])['passed']


def test_true_lag_still_breaches_the_same_budget():
    snapshots = sample_clock('ros',0.3)
    assert not evaluate(snapshots,fail_on='never',forbid_kinds=['stale_data'])['passed']
    for snap in snapshots:
        hits = [i for i in snap['issues'] if i['kind']=='stale_data']
        assert len(hits)==1 and hits[0]['related_topics']==['/pose']
        assert hits[0]['evidence']==['p95 header age: 300 ms','expected within: 100 ms']


@pytest.mark.parametrize('clock', [None,0.0])
def test_unstarted_ros_clock_age_stays_unknown(clock):
    analyzer = BagAnalyzer(POSE,Thresholds(),age_clock='ros')
    if clock is not None:
        analyzer.update_ros_clock(clock)
    analyzer.add_message('/pose',T0,100,stamp=100)
    assert analyzer.snapshots()[0]['topics'][0]['header_age_ms'] is None


def test_paused_clock_and_backward_reset_follow_recorded_time():
    analyzer = BagAnalyzer(POSE,Thresholds(),age_clock='ros')
    analyzer.update_ros_clock(100)
    for i in range(3):
        analyzer.add_message('/pose',T0+i,100,stamp=99.98)
    assert analyzer.snapshots(window=1)[-1]['topics'][0]['header_age_p95_ms']==20
    analyzer.update_ros_clock(5)
    analyzer.add_message('/pose',T0+3,100,stamp=4.7)
    assert analyzer.snapshots(window=1)[-1]['topics'][0]['header_age_p95_ms']==300
    analyzer.update_ros_clock(None)
    analyzer.add_message('/pose',T0+4,100,stamp=4.7)
    assert analyzer.snapshots(window=1)[-1]['topics'][0]['header_age_p95_ms'] is None
    assert analyzer.unreadable_clock_messages==1


def install_reader(monkeypatch,topic_types,events):
    class Reader:
        def open(self,*args):
            self.rows=list(events)
        def get_all_topics_and_types(self):
            return [NS(name=n,type=t) for n,t in topic_types.items()]
        def has_next(self):
            return bool(self.rows)
        def read_next(self):
            return self.rows.pop(0)
    class Stamped:
        @staticmethod
        def get_fields_and_field_types():
            return {'header':'std_msgs/Header'}
    def get_message(t):
        if t.endswith('/PoseStamped'):
            return Stamped
        raise ImportError('binding intentionally unavailable')
    def deserialize(*args):
        raise AssertionError('headers and clocks need no full deserialization')
    modules={
        'rosbag2_py':NS(SequentialReader=Reader,StorageOptions=lambda **kw:NS(**kw),ConverterOptions=lambda **kw:NS(**kw)),
        'rclpy':types.ModuleType('rclpy'),
        'rclpy.serialization':NS(deserialize_message=deserialize),
        'rosidl_runtime_py':types.ModuleType('rosidl_runtime_py'),
        'rosidl_runtime_py.utilities':NS(get_message=get_message),
    }
    for n,m in modules.items():
        monkeypatch.setitem(sys.modules,n,m)


@pytest.mark.parametrize('mode,selected', [('auto','ros'),('ros','ros'),('bag','bag')])
def test_reader_selects_clock_and_retains_recording_provenance(monkeypatch,tmp_path,mode,selected):
    from ros_graph_debugger.cli import main
    types_=dict(POSE,**{'/clock':'rosgraph_msgs/msg/Clock'})
    events=[('/clock',cdr_time(0),int(T0*1e9)),
            ('/pose',cdr_time(99,980_000_000),int((T0+0.1)*1e9)),
            ('/clock',cdr_time(100),int((T0+1)*1e9)),
            ('/pose',cdr_time(99,980_000_000),int((T0+1.1)*1e9)),
            ('/clock',cdr_time(101,0,'>'),int((T0+2)*1e9)),
            ('/pose',cdr_time(100,980_000_000),int((T0+2.1)*1e9))]
    install_reader(monkeypatch,types_,events)
    out=tmp_path/'run.rgd.json'
    assert main(['bag','analyze','fixture.db3','--out',str(out),'--age-clock',mode])==0
    header,snaps=read_recording(str(out))
    assert header['header_age_clock']=={'requested':mode,'selected':selected,
        'clock_messages':3 if selected=='ros' else 0,'unreadable_clock_messages':0}
    final=next(t for t in snaps[-1]['topics'] if t['name']=='/pose')
    assert final['header_age_p95_ms']==20 if selected=='ros' else final['header_age_p95_ms']>1e9


@pytest.mark.parametrize('clock_type', [None,'std_msgs/msg/Float64'])
def test_explicit_ros_requires_standard_clock_and_auto_falls_back(monkeypatch,clock_type):
    types_=dict(POSE)
    if clock_type:
        types_['/clock']=clock_type
    install_reader(monkeypatch,types_,[('/pose',cdr_time(1),int(T0*1e9))])
    with pytest.raises(ValueError,match='requires a recorded /clock'):
        analyze_bag('fixture.db3',Thresholds(),age_clock='ros')
    assert analyze_bag('fixture.db3',Thresholds()).age_clock=='bag'


def test_bad_clock_payload_is_unknown_without_falling_back_to_system_time(monkeypatch):
    types_=dict(POSE,**{'/clock':'rosgraph_msgs/msg/Clock'})
    install_reader(monkeypatch,types_,[('/clock',b'bad',int(T0*1e9)),
                                     ('/pose',cdr_time(1),int((T0+0.1)*1e9))])
    analyzer=analyze_bag('fixture.db3',Thresholds())
    assert analyzer.unreadable_clock_messages==1
    pose=next(t for t in analyzer.snapshots()[0]['topics'] if t['name']=='/pose')
    assert pose['header_age_ms'] is None
