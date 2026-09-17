# Docker Deployment

This project exposes the FastAPI application from `api.py` on port `8501`.

## Files

- `Dockerfile` builds the Python API image and installs Microsoft ODBC Driver 17 for SQL Server.
- `docker-compose.yml` builds/runs the service and maps `8501:8501`.
- `.dockerignore` keeps local caches, Git data, `node_modules`, generated maps, and real `.env` secrets out of the image build context.
- `.env.example` documents the runtime environment variables.

## Run

Create or update your local `.env` file:

```env
API_KEY=your_openrouteservice_api_key
DATABASE_URL=mssql+pyodbc://username:password@sql-server-host:1433/Fuel_Station?driver=ODBC+Driver+17+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes
```

Then start Docker Desktop and run:

```bash
docker compose up --build
```

Open:

```text
http://localhost:8501/docs
```

## SQL Server Notes

Inside Docker, `localhost` means the container itself, not your Windows host. Use a reachable SQL Server hostname or IP in `DATABASE_URL`.

If SQL Server runs on the same Windows machine, common options are:

```env
DATABASE_URL=mssql+pyodbc://username:password@host.docker.internal:1433/Fuel_Station?driver=ODBC+Driver+17+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes
```

For easiest Linux container deployment, prefer SQL authentication with `username:password`. Windows trusted connections usually require extra Kerberos/domain configuration in Linux containers.
