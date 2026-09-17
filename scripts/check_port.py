import os, socket
from dotenv import load_dotenv
load_dotenv()
url = os.getenv('DATABASE_URL')
print('DATABASE_URL:', bool(url))
if not url:
    raise SystemExit('No DATABASE_URL')

# crude parse for host,port from mssql+pyodbc://...@HOST,PORT/DB
host_port = None
if 'mssql+pyodbc://' in url:
    after = url.split('@')[-1]
    host_and_rest = after.split('/')[0]
    if ',' in host_and_rest:
        host, port = host_and_rest.split(',',1)
    else:
        host = host_and_rest
        port = '1433'
    host = host.strip()
    port = int(port)
    print('Host:', host, 'Port:', port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
        print('Port is open')
    except Exception as e:
        print('Port check failed:', e)
    finally:
        s.close()
else:
    print('DATABASE_URL not in expected format')
