FROM python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py

ENV PORT=8080
ENV STARTUP_DELAY_SECONDS=40

USER 65532:65532
EXPOSE 8080

CMD ["python", "-u", "/app/server.py"]
