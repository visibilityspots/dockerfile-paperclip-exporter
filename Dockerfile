FROM python:3.15.2-alpine3.24
# apk upgrade: the python image lags behind the alpine package repo.
# The exporter is stdlib only, so pip (and the CVEs in its vendored packages) goes.
RUN apk upgrade --no-cache \
    && pip uninstall -y pip \
    && adduser -D -H -u 10001 exporter
WORKDIR /opt/paperclip-exporter
COPY ./exporter.py .
USER exporter
EXPOSE 9878
ENTRYPOINT ["python3", "-I", "-u", "exporter.py"]
