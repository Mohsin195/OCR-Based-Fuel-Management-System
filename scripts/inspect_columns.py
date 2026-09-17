import pyodbc
conn_str = 'DRIVER={ODBC Driver 17 for SQL Server};SERVER=DESKTOP-7K48LCE;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;'
conn = pyodbc.connect(conn_str)
cur = conn.cursor()
cur.execute("SELECT TOP 1 * FROM dbo.LovesLocations")
print('columns:', [d[0] for d in cur.description])
row = cur.fetchone()
print('row:', row)
conn.close()
