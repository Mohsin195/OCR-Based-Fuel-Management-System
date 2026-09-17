import os
import pyodbc
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

print('Using DATABASE_URL:', bool(DATABASE_URL))

try:
    conn = pyodbc.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('SELECT TOP 20 city, address, latitude, longitude FROM LovesLocations')
    rows = cursor.fetchall()
    print(f'Fetched {len(rows)} rows')
    for r in rows[:20]:
        print({'city': r[0], 'address': r[1], 'lat': r[2], 'lon': r[3]})
    conn.close()
except Exception as e:
    print('Error querying database:', e)
