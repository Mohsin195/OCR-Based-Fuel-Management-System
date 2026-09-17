import pyodbc

candidates = [
    'DRIVER={ODBC Driver 17 for SQL Server};SERVER=DESKTOP-7K48LCE;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
    'DRIVER={ODBC Driver 18 for SQL Server};SERVER=DESKTOP-7K48LCE;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
    'DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
    'DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
    'DRIVER={ODBC Driver 17 for SQL Server};SERVER=.;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
    'DRIVER={ODBC Driver 18 for SQL Server};SERVER=.;DATABASE=Fuel_Station;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;',
]

for conn_str in candidates:
    print('TRY', conn_str)
    try:
        conn = pyodbc.connect(conn_str, timeout=3)
        cur = conn.cursor()
        cur.execute('SELECT DB_NAME()')
        print('OK', cur.fetchone()[0])
        conn.close()
        break
    except Exception as e:
        print('FAIL', e)
