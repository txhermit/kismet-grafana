FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kismet_exporter.py .
EXPOSE 9102
CMD ["python3", "kismet_exporter.py"]
