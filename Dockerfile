FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY main.py /usr/local/bin/docat
RUN chmod +x /usr/local/bin/docat

ENTRYPOINT ["docat"]
CMD ["--help"]
