FROM python:3.9-slim
RUN apt-get update && apt-get install -y iproute2 curl
WORKDIR /app
COPY topology.py requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir
CMD ["python", "topology.py"]