"""Pilot: original recorded Clock and 7 MB images; exact delivery gate."""
import argparse
from collections import Counter
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import random
import sqlite3
import struct
import sys
import time
import urllib.request

sys.path.insert(0, str(Path.cwd() / 'test/benchmarks'))
from run_observer import group_stats, network, percentile, save
sys.path.insert(0, str(Path.cwd() / 'test/integration'))
from run_scenario import Processes

IMAGE = '/sensing/camera/front/image'
START, END, DRAIN = 10, 20, 22


def stamp(raw):
    assert len(raw) >= 12 and raw[:2] in (b'\x00\x00', b'\x00\x01')
    sec, nano = struct.unpack_from('<iI' if raw[1] == 1 else '>iI', raw, 4)
    assert sec >= 0 and nano < 10**9
    return sec * 10**9 + nano


def source(bag):
    files = list(bag.glob('*.db3'))
    assert len(files) == 1
    with closing(sqlite3.connect(f'file:{files[0]}?mode=ro', uri=True)) as db:
        start = db.execute('SELECT min(timestamp) FROM messages').fetchone()[0]
        types = dict(db.execute('SELECT name,type FROM topics'))
        assert types[IMAGE] == 'sensor_msgs/msg/Image' and types['/clock'] == 'rosgraph_msgs/msg/Clock'
        rows = [{ 'stamp_ns': stamp(raw), 'offset': (t-start)/1e9, 'bytes': n }
                for t,raw,n in db.execute('SELECT timestamp,substr(data,1,12),length(data) FROM messages '
                    'WHERE topic_id=(SELECT id FROM topics WHERE name=?) ORDER BY timestamp,id', (IMAGE,))]
    assert rows and len({r['stamp_ns'] for r in rows}) == len(rows), 'source image stamps must be unique'
    assert max(r['offset'] for r in rows) > DRAIN
    return rows


def receiver(output, rows, reliability):
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import Image
    lookup = {r['stamp_ns']: r for r in rows}
    received, last = [], {'offset': None}
    rclpy.init()
    node = Node('image_benchmark_receiver', parameter_overrides=[Parameter('use_sim_time', value=True)])
    def callback(raw, info):
        key = stamp(raw)
        assert key in lookup and len(raw) == lookup[key]['bytes']
        now = node.get_clock().now().nanoseconds
        received.append({'stamp_ns': key, 'bytes': len(raw), 'offset': lookup[key]['offset'],
            'receipt_ns': info.get('received_timestamp', 0), 'source_ns': info.get('source_timestamp', 0),
            'signed_ros_age_ms': (now-key)/1e6 if now > 0 else None})
        last['offset'] = lookup[key]['offset']
    sub = node.create_subscription(Image, IMAGE, callback,
        QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE if reliability == 'reliable' else ReliabilityPolicy.BEST_EFFORT), raw=True)
    deadline, updated, saved = time.monotonic()+70, 0, False
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
            if time.monotonic()-updated >= 0.1:
                save(output/'progress.json', {'ready': True, 'count': len(received), 'offset': last['offset'],
                    'image_publishers': node.count_publishers(IMAGE), 'clock_publishers': node.count_publishers('/clock'),
                    'image_publisher_reliability': [e.qos_profile.reliability.name for e in node.get_publishers_info_by_topic(IMAGE)]})
                updated = time.monotonic()
            if last['offset'] is not None and last['offset'] >= DRAIN and not saved:
                save(output/'receiver.json', received)
                saved = True
        raise TimeoutError('receiver measurement did not finish/stop within budget')
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def resume_player():
    import rclpy
    from rclpy.node import Node
    from rosbag2_interfaces.srv import Resume
    rclpy.init()
    node = Node('image_benchmark_controller')
    try:
        client = node.create_client(Resume, '/rosbag2_player/resume')
        deadline = time.monotonic()+15
        while not client.wait_for_service(timeout_sec=0.1):
            assert time.monotonic() < deadline, 'player resume service unavailable'
        future = client.call_async(Resume.Request())
        while not future.done():
            assert time.monotonic() < deadline, 'player resume timeout'
            rclpy.spin_once(node, timeout_sec=0.1)
        assert future.result() is not None
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def case(output, mode, args, rows):
    output.mkdir()
    processes = Processes(output)
    snapshots, samples, commands = [], [], {}
    begin = end = None
    base = f'http://127.0.0.1:{args.port}'
    def launch(name, command):
        commands[name] = command
        processes.start(name, command)
    def snapshot():
        with urllib.request.urlopen(base+'/api/v1/snapshot', timeout=1) as r:
            return json.load(r)
    try:
        launch('receiver', [sys.executable, __file__, '--receiver', str(output), '--source-rows', str(args.output/'source-rows.json'), '--reliability', args.reliability])
        if mode != 'baseline':
            command = ['ros2','run','ros_graph_debugger','agent','--no-browser','--profile','autoware','--port',str(args.port)]
            command += ['--no-probe'] if mode == 'no_probe' else ['--probe-topic','/clock']
            if mode == 'image':
                command += ['--probe-topic', IMAGE]
            launch('agent', command+['--ros-args','-p','use_sim_time:=true'])
        player_command = ['ros2','bag','play',str(args.bag),'--start-paused','--topics','/clock',IMAGE]
        if args.reliability == 'reliable':
            qos = output/'publisher-qos.yaml'
            qos.write_text(IMAGE + ':\n  reliability: reliable\n  durability: volatile\n  history: keep_last\n  depth: 10\n')
            player_command += ['--qos-profile-overrides-path',str(qos)]
        launch('player', player_command)
        save(output/'commands.json', commands)
        deadline = time.monotonic()+30
        while True:
            processes.check()
            try:
                p = json.loads((output/'progress.json').read_text())
                assert p['ready'] and p['image_publishers'] >= 1 and p['clock_publishers'] == 1
                if args.reliability == 'reliable' and p['image_publisher_reliability'] != ['RELIABLE']:
                    raise RuntimeError('reliable receiver requires explicit compatible image publisher QoS')
                if mode != 'baseline':
                    snapshot()
                break
            except (OSError, ValueError, AssertionError):
                assert time.monotonic() < deadline, 'DDS/HTTP readiness timeout'
                time.sleep(0.05)
        resume_player()
        deadline, next_http = time.monotonic()+40, time.monotonic()
        while time.monotonic() < deadline:
            processes.check()
            p = json.loads((output/'progress.json').read_text())
            sample = {'wall':time.monotonic(), 'offset': p['offset'], 'groups': group_stats({n:c.pid for n,c in processes.children})}
            samples.append(sample)
            if mode != 'baseline' and time.monotonic() >= next_http:
                snapshots.append({'offset':p['offset'],'snapshot':snapshot()})
                next_http += 1
            if p['offset'] is not None and p['offset'] >= START and begin is None:
                begin = sample
            if p['offset'] is not None and p['offset'] >= END and end is None:
                end = sample
            if (output/'receiver.json').exists():
                break
            time.sleep(0.05)
        assert begin and end and (output/'receiver.json').exists(), 'incomplete interval'
        rx = json.loads((output/'receiver.json').read_text())
        wanted = Counter(r['stamp_ns'] for r in rows if START+0.05 < r['offset'] <= END-0.05)
        got = Counter(r['stamp_ns'] for r in rx)
        assert all(r['receipt_ns'] > 0 and r['source_ns'] > 0 for r in rx), 'middleware timestamps unavailable'
        missing, duplicate = wanted-got, Counter({k:got[k]-v for k,v in wanted.items() if got[k]>v})
        elapsed = end['wall']-begin['wall']
        resources = {name:{'cpu_percent_one_core':(end['groups'][name]['cpu_seconds']-begin['groups'][name]['cpu_seconds'])/elapsed*100,
                          'rss_max_bytes': max(s['groups'][name]['rss_bytes'] for s in samples if s['offset'] is not None and START <= s['offset'] <= END)}
                     for name in begin['groups']}
        roi = [r for r in rx if r['stamp_ns'] in wanted]
        result = {'mode':mode,'receiver_reliability':args.reliability,'elapsed':elapsed,'source_frames':sum(wanted.values()),'missing':sum(missing.values()),'duplicates':sum(duplicate.values()),
                  'resources':resources,'signed_dds_latency_p95_ms':percentile([(r['receipt_ns']-r['source_ns'])/1e6 for r in roi],0.95),
                  'signed_ros_age_p95_ms':percentile([r['signed_ros_age_ms'] for r in roi if r['signed_ros_age_ms'] is not None],0.95)}
        save(output/'result.json', result)
        print(json.dumps(result),flush=True)
        assert not missing and not duplicate, 'exact source image delivery contract failed'
        if mode == 'image':
            measured = [s for s in snapshots if s['offset'] is not None and START <= s['offset'] < END]
            assert len(measured) >= 8
            for observation in measured:
                t = next(t for t in observation['snapshot']['topics'] if t['name']==IMAGE)
                assert t['probed'] and t['rate_hz'] is not None and t['rate_hz'] > 0 and t['header_age_p95_ms'] is not None
            result['image_probe_rate_range_hz'] = [min(next(t['rate_hz'] for t in s['snapshot']['topics'] if t['name']==IMAGE) for s in measured), max(next(t['rate_hz'] for t in s['snapshot']['topics'] if t['name']==IMAGE) for s in measured)]
            save(output/'result.json', result)
        return result
    finally:
        try:
            save(output/'samples.json', samples)
            save(output/'snapshots.json', snapshots)
        finally:
            processes.close()
        cleanup = group_stats({n:c.pid for n,c in processes.children})
        save(output/'cleanup.json', cleanup)
        assert all(not s['pids'] for s in cleanup.values()), cleanup


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bag',type=Path)
    parser.add_argument('--source-label',default='unknown')
    parser.add_argument('--source-metadata',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--receiver',type=Path)
    parser.add_argument('--source-rows',type=Path)
    parser.add_argument('--repetitions',type=int,default=1)
    parser.add_argument('--reliability',choices=['best_effort','reliable'],default='best_effort')
    parser.add_argument('--port',type=int,default=3963)
    parser.add_argument('--modes',nargs='+',choices=['baseline','no_probe','clock','image'],default=['baseline','no_probe','clock','image'])
    args=parser.parse_args()
    if args.receiver:
        receiver(args.receiver,json.loads(args.source_rows.read_text()),args.reliability)
        return
    if args.repetitions < 1:
        parser.error('--repetitions must be positive')
    if args.bag is None or args.output is None:
        parser.error('--bag and --output are required')
    os.environ['ROS_DOMAIN_ID']='97'
    args.output.mkdir(exist_ok=False)
    rows=source(args.bag)
    save(args.output/'source-rows.json',rows)
    save(args.output/'protocol.json', {'source_commit':args.source_label, 'source_metadata':json.loads(args.source_metadata.read_text()) if args.source_metadata else None, 'cpu_affinity':sorted(os.sched_getaffinity(0)), 'production_sha256':{n:hashlib.sha256((Path.cwd()/'ros_graph_debugger'/n).read_bytes()).hexdigest() for n in ('agent.py','node.py','probe_executor.py')}, 'clock':'original recorded /clock, no generated clock','image':IMAGE, 'receiver_reliability':args.reliability, 'source_frames':len(rows), 'measurement_source_offsets':[START,END], 'exact_delivery_roi':[START+0.05,END-0.05], 'repetitions':args.repetitions, 'default_rmw':__import__('rclpy.utilities',fromlist=['get_rmw_implementation_identifier']).get_rmw_implementation_identifier()})
    results=[]
    rng=random.Random(20261012)
    for repetition in range(args.repetitions):
        modes=list(args.modes);rng.shuffle(modes)
        for mode in modes:
            results.append(case(args.output/f'{repetition}-{mode}',mode,args,rows))
    save(args.output/'results.json',results)


if __name__ == '__main__':
    main()
