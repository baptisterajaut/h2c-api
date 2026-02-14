FROM python:3.12-slim

RUN pip install --no-cache-dir pyyaml

COPY h2c_api.py /app/h2c_api.py
WORKDIR /app

EXPOSE 6443

CMD ["python3", "h2c_api.py"]
