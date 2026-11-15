FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgdal-dev gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8051
# The DEM and waypoint log come from /app/data (a shared volume, see deploy/planner-host).
CMD ["python", "app.py", "--host", "0.0.0.0", "--no-browser", "--port", "8051"]
