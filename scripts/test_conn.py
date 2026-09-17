from app_2 import get_db_connection

conn = get_db_connection()
print('get_db_connection returned:', conn)
if conn:
    cur = conn.cursor()
    cur.execute('SELECT TOP 1 city, address, latitude, longitude FROM LovesLocations')
    row = cur.fetchone()
    print('sample row:', row)
    conn.close()
