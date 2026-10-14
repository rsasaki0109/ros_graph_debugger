"""Real invalid processing samples must not break HTTP or displace valid statistics."""
import argparse,json,math,os,signal,sys,threading,time,urllib.error,urllib.request
from pathlib import Path
from run_scenario import Processes
parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--role',choices=['publisher','receiver'])
a=parser.parse_args();root=a.output
os.environ.setdefault('ROS_DOMAIN_ID','99')
TOPIC='/control/invalid_processing/debug/processing_time_ms'
if a.role:
 import rclpy
 from rclpy.node import Node
 from rclpy.executors import SingleThreadedExecutor,ExternalShutdownException
 from std_msgs.msg import Float64
 rclpy.init()
 executor=SingleThreadedExecutor()
 server=None;web_thread=None
 nodes=[]
 if a.role=='publisher':
  node=Node('invalid_processing',namespace='/control');nodes.append(node);executor.add_node(node)
  pub=node.create_publisher(Float64,TOPIC,10)
  def send():
   if pub.get_subscription_count()<2:return
   phase=(root/'phase').read_text().strip()
   value={'high':50.0,'low':1.0,'inf':float('inf'),'nan':float('nan'),'negative':-1.0,'huge':sys.float_info.max}[phase]
   pub.publish(Float64(data=value))
  node.create_timer(.02,send)
 else:
  import uvicorn
  from ros_graph_debugger.config import ProbeConfig,Thresholds
  from ros_graph_debugger.model import RuntimeGraphStore
  from ros_graph_debugger.node import DebuggerNode
  from ros_graph_debugger.profile import apply_profile,load_profile
  from ros_graph_debugger.paths import find_profile,find_web_dir
  from ros_graph_debugger.server import create_app
  store=RuntimeGraphStore();thresholds=Thresholds();profile,_=load_profile(find_profile('autoware'));apply_profile(thresholds,profile)
  debugger=DebuggerNode(store,ProbeConfig(enabled=False),thresholds,None,statistics_topics={},lifecycle=False,processing_time_patterns=['/debug/processing_time_ms$'])
  monitor=Node('invalid_processing_monitor');nodes.extend([debugger,monitor]);executor.add_node(debugger);executor.add_node(monitor)
  counts={}
  def seen(msg):
   value=msg.data
   token='nan' if math.isnan(value) else 'inf' if math.isinf(value) else 'negative' if value<0 else 'huge' if value==sys.float_info.max else 'high' if value==50 else 'low'
   counts[token]=counts.get(token,0)+1
   temporary=root/'received.tmp';temporary.write_text(json.dumps(counts));temporary.replace(root/'received.json')
  monitor.create_subscription(Float64,TOPIC,seen,100)
  server=uvicorn.Server(uvicorn.Config(create_app(store,find_web_dir(),thresholds=thresholds),host='127.0.0.1',port=3988,log_level='warning',lifespan='off'))
  web_thread=threading.Thread(target=server.run);web_thread.start()
 try:
  while rclpy.ok():executor.spin_once(timeout_sec=.05)
 except (KeyboardInterrupt,ExternalShutdownException):pass
 finally:
  if server:
   server.should_exit=True;web_thread.join(timeout=5);assert not web_thread.is_alive()
  executor.shutdown()
  for node in nodes:node.destroy_node()
  if rclpy.ok():rclpy.shutdown()
 sys.exit(0)

root.mkdir(parents=True,exist_ok=False)
def phase(value):
 temporary=root/'phase.tmp';temporary.write_text(value);temporary.replace(root/'phase')
phase('high')
def terminate(signum,_frame):
 signal.signal(signum,signal.SIG_IGN);raise SystemExit(128+signum)
def expired(_signum,_frame):raise TimeoutError('processing validity scenario exceeded 60 seconds')
signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,60)
processes=Processes(root)
def snapshot():
 with urllib.request.urlopen('http://127.0.0.1:3988/api/v1/snapshot',timeout=2) as r:return json.load(r)
def poll(fn):
 deadline=time.monotonic()+30
 last=None
 while True:
  processes.check()
  try:
   result=fn();assert result;return result
  except (OSError,ValueError,AssertionError,urllib.error.HTTPError) as exc:last=repr(exc)
  assert time.monotonic()<deadline,last
  time.sleep(.05)
try:
 processes.start('receiver',[sys.executable,__file__,'--role','receiver','--output',str(root)])
 processes.start('publisher',[sys.executable,__file__,'--role','publisher','--output',str(root)])
 def high_ready():
  s=snapshot();c=next(v for v in s['callbacks'] if v['topic']==TOPIC)
  assert c['count']>=10 and c['p95_ms']==50 and any(i['kind']=='slow_callback' for i in s['issues'])
  (root/'valid-snapshot.json').write_text(json.dumps(s));return c
 # StopIteration is a normal discovery condition, not an assertion relaxation.
 def ready():
  try:return high_ready()
  except StopIteration:return False
 poll(ready)
 results={}
 for invalid in ('inf','nan','negative'):
  phase(invalid)
  poll(lambda:json.loads((root/'received.json').read_text()).get(invalid,0)>=10)
  def still_high():
   s=snapshot();c=next(v for v in s['callbacks'] if v['topic']==TOPIC)
   assert c['p95_ms']==c['max_ms']==c['mean_ms']==50 and c['source']=='application_report'
   assert any(i['kind']=='slow_callback' and i['related_topics']==[TOPIC] for i in s['issues'])
   json.dumps(s,allow_nan=False)
   (root/(invalid+'-snapshot.json')).write_text(json.dumps(s));return c
  results[invalid]=poll(still_high)
 phase('huge')
 poll(lambda:json.loads((root/'received.json').read_text()).get('huge',0)>=120)
 def huge_ready():
  s=snapshot();c=next(v for v in s['callbacks'] if v['topic']==TOPIC)
  assert c['count']==100 and c['p95_ms']==c['max_ms']==c['mean_ms']==sys.float_info.max
  json.dumps(s,allow_nan=False)
  (root/'huge-snapshot.json').write_text(json.dumps(s));return c
 results['huge']=poll(huge_ready)
 phase('low')
 poll(lambda:json.loads((root/'received.json').read_text()).get('low',0)>=120)
 def low_ready():
  s=snapshot();c=next(v for v in s['callbacks'] if v['topic']==TOPIC)
  assert c['count']==100 and c['p95_ms']==c['max_ms']==c['mean_ms']==1
  assert not any(i['kind']=='slow_callback' and i['related_topics']==[TOPIC] for i in s['issues'])
  (root/'low-snapshot.json').write_text(json.dumps(s));return c
 results['low']=poll(low_ready)
 (root/'results.json').write_text(json.dumps(dict(phases=results,received=json.loads((root/'received.json').read_text()),http_snapshot_valid=True),indent=2)+'\n')
 print('real bad values ignored; large finite mean and healthy recovery remain JSON-safe',flush=True)
finally:
 processes.close()
 groups={c.pid for _,c in processes.children};remaining=[]
 for path in Path('/proc').glob('[0-9]*/stat'):
  try:
   fields=path.read_text().rsplit(')',1)[1].split()
   if int(fields[2]) in groups and fields[0]!='Z':remaining.append(int(path.parent.name))
  except (OSError,ValueError):pass
 cleanup=dict(direct_child_returns={n:c.poll() for n,c in processes.children},active_owned_group_members=remaining)
 (root/'cleanup.json').write_text(json.dumps(cleanup,indent=2)+'\n')
 signal.setitimer(signal.ITIMER_REAL,0)
 assert not remaining and all(c.returncode==0 for _,c in processes.children),cleanup
