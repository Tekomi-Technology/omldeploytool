FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge_app.py .
COPY ami_collector.py .
CMD ["python", "bridge_app.py"]
