FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgdal-dev gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /app/data
EXPOSE 8050
# SYNTHETIC=1 uses built-in test terrain (no 90 MB download). Otherwise the real Jezero DEM is used.
CMD ["sh", "-c", "if [ -n \"$SYNTHETIC\" ]; then python app.py --host 0.0.0.0 --no-browser --synthetic; else python get_data.py && python app.py --host 0.0.0.0 --no-browser; fi"]
