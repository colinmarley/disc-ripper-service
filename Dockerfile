FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install makemkv from the same PPA used on the host, plus encode tools and Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common gpg-agent ca-certificates \
    && add-apt-repository ppa:heyarje/makemkv-beta \
    && apt-get update && apt-get install -y --no-install-recommends \
    makemkv-bin \
    makemkv-oss \
    ffmpeg \
    handbrake-cli \
    python3 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

EXPOSE 8083

CMD ["python", "main.py"]
