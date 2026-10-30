FROM python:3.11-slim-bookworm

# libgtk-3-0 … libxtst6 + squashfs-tools: the Naukri Launcher (Electron AppImage,
# app/services/naukri_launcher.py) links against GTK/desktop libs, and is unpacked
# with unsquashfs because containers have no FUSE.
RUN rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    xvfb \
    x11vnc \
    supervisor \
    websockify \
    libgtk-3-0 \
    dbus \
    libnotify4 \
    libxss1 \
    libsecret-1-0 \
    libxtst6 \
    fonts-liberation \
    libgl1-mesa-dri \
    libgl1-mesa-glx \
    squashfs-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# --no-cache-dir: don't bake pip's download cache (~114 MB) into the image layer.
RUN pip install --no-cache-dir -r requirements.txt

# chromium here installs full Chromium + chromium-headless-shell. Both are needed:
# PlaywrightService launches headless=True with no channel, which in PW 1.49
# resolves to the headless-shell binary; the headful/persistent path uses full
# Chromium. --no-shell was tried and broke the headless path — do not re-add it.
RUN playwright install chromium

COPY . .
COPY supervisord.conf /etc/supervisor/conf.d/harvest.conf
# Records the Naukri Launcher's auto-login hand-off (see the script). sed strips
# CRLF in case the checkout converted line endings on Windows.
RUN sed -i 's/\r$//' docker/xdg-open && install -m 0755 docker/xdg-open /usr/local/bin/xdg-open

ENV DISPLAY=:99

EXPOSE 8000 6080

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/harvest.conf"]