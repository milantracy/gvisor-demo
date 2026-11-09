# The systemd image (images/systemd-ubuntu) plus Carbonyl, a Chromium build
# that renders pages, video included, in the terminal:
# https://github.com/fathyb/carbonyl
FROM gvisor-demo/systemd-ubuntu

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates curl unzip fonts-dejavu-core \
    libnss3 libexpat1 libfontconfig1 libasound2t64 libdbus-1-3 \
    libxkbcommon0 libgbm1 libdrm2 libglib2.0-0t64 && \
    apt-get clean

ARG CARBONYL=https://github.com/fathyb/carbonyl/releases/download/v0.0.3/carbonyl.linux-amd64.zip
RUN curl -fsSL -o /tmp/carbonyl.zip "$CARBONYL" && \
    unzip -q /tmp/carbonyl.zip -d /opt && \
    mv /opt/carbonyl-0.0.3 /opt/carbonyl && \
    ln -s /opt/carbonyl/carbonyl /usr/local/bin/carbonyl && \
    rm /tmp/carbonyl.zip

CMD ["/sbin/init"]
