"""Independent native DDS timestamps with a condition-controlled observer stall."""
import argparse,json,os
from pathlib import Path
import sys,time
sys.path.insert(0,str(Path.cwd()/'test/integration'))
from run_scenario import Processes
parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--role',choices=['publisher','receiver'])
a=parser.parse_args()
os.environ.setdefault('ROS_DOMAIN_ID','97')
if a.role:
 import rclpy
 from rclpy.node import Node
 from rclpy.executors import ExternalShutdownException
 from rclpy.parameter import Parameter
 from rclpy.qos import QoSProfile,ReliabilityPolicy
 from rclpy.serialization import deserialize_message
 from std_msgs.msg import UInt64
 from ros_graph_debugger.probe_executor import ProbeExecutor,with_message_info
 from ros_graph_debugger.node import _TopicProbe
 rclpy.init()
 node=Node('dispatch_'+a.role,parameter_overrides=[Parameter('use_sim_time',value=True)])
 ex=ProbeExecutor();ex.add_node(node)
 rows=[]
 probe=_TopicProbe(window=5)
 qos=QoSProfile(depth=100,reliability=ReliabilityPolicy.RELIABLE)
 if a.role=='publisher':
  pub=node.create_publisher(UInt64,'/dispatch/counter',qos)
  def tick():
   if pub.get_subscription_count()!=1 or len(rows)>=30:return
   if rows and not (a.output/'blocked').exists():return
   seq=len(rows)
   rows.append({'seq':seq,'wall_ns':time.time_ns(),'monotonic':time.monotonic()})
   pub.publish(UInt64(data=seq))
   if len(rows)==12:(a.output/'release').write_text('12 sent')
   if len(rows)==30:(a.output/'publisher.json').write_text(json.dumps(rows))
  from rclpy.clock import Clock,ClockType
  node.create_timer(0.05,tick,clock=Clock(clock_type=ClockType.STEADY_TIME))
 else:
  def cb(raw,info):
   entry=time.time_ns();mono=time.monotonic();seq=deserialize_message(raw,UInt64).data
   probe.add(len(raw),received_timestamp_ns=info['received_timestamp'],callback_timestamp_ns=entry)
   rows.append({'seq':seq,'callback_ns':entry,'callback_monotonic':mono,'ros_time_ns':node.get_clock().now().nanoseconds,'info':info,'delay_ms':(entry-info['received_timestamp'])/1e6,'probe_metrics':probe.metrics()})
   if seq==0:
    (a.output/'blocked').write_text('entered callback 0')
    deadline=time.monotonic()+5
    while not (a.output/'release').exists():
     assert time.monotonic()<deadline,'publisher did not release callback'
     time.sleep(0.005)
   if len(rows)==30:(a.output/'receiver.json').write_text(json.dumps(rows))
  node.create_subscription(UInt64,'/dispatch/counter',with_message_info(cb),qos,raw=True)
 try:
  deadline=time.monotonic()+12
  while time.monotonic()<deadline:ex.spin_once(timeout_sec=0.02)
  raise TimeoutError('parent did not finish before child budget')
 except (KeyboardInterrupt,ExternalShutdownException):
  pass
 finally:
  ex.shutdown();node.destroy_node()
  if rclpy.ok():rclpy.shutdown()
else:
 a.output.mkdir(exist_ok=False)
 p=Processes(a.output)
 try:
  p.start('receiver',[sys.executable,__file__,'--output',str(a.output),'--role','receiver'])
  p.start('publisher',[sys.executable,__file__,'--output',str(a.output),'--role','publisher'])
  deadline=time.monotonic()+10
  while not all((a.output/f'{n}.json').exists() for n in ('receiver','publisher')):
   p.check();assert time.monotonic()<deadline,'native capability timeout';time.sleep(0.02)
  rx=json.loads((a.output/'receiver.json').read_text());tx=json.loads((a.output/'publisher.json').read_text())
  assert [r['seq'] for r in rx]==list(range(30))
  assert all(r['ros_time_ns']==0 for r in rx),'ROS Clock must remain uninitialized'
  assert all(r['info']['received_timestamp']>0 for r in rx)
  delayed=[r for r in rx if 1<=r['seq']<=5]
  assert max(r['delay_ms'] for r in delayed)>300,'receive timestamp does not expose controlled backlog'
  assert all(r['delay_ms']>=0 for r in rx)
  held=next(r['probe_metrics'] for r in rx if r['seq']==5)
  recovered=rx[-1]['probe_metrics']
  assert held['probe_callback_delay_p95_ms']>300,held
  assert recovered['probe_callback_delay_p95_ms']<100,recovered
  assert held['probe_callback_delay_samples']==held['probe_callback_delay_window_samples']==5
  assert recovered['probe_callback_delay_samples']==recovered['probe_callback_delay_window_samples']==5
  assert held['rate_hz']>10 and recovered['rate_hz']>10,'DDS rate must retain its contract despite observer wait'
  summary={'rmw':__import__('rclpy').get_rmw_implementation_identifier(),'received':len(rx),'delayed_ms':[r['delay_ms'] for r in delayed],'tail_ms':[r['delay_ms'] for r in rx[-5:]],'ros_clock_uninitialized':True,'held_probe':held,'recovered_probe':recovered}
  (a.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))
 finally:
  p.close()
  reaped={}
  for name,child in p.children:
   try:os.killpg(child.pid,0)
   except ProcessLookupError:reaped[name]=True
   else:reaped[name]=False
  (a.output/'cleanup.json').write_text(json.dumps(reaped))
  assert all(reaped.values()),reaped

if not a.role:
 codes={name:child.returncode for name,child in p.children}
 (a.output/'exit-codes.json').write_text(json.dumps(codes))
 assert all(code==0 for code in codes.values()),codes
