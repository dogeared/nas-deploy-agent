FROM python:3.13-alpine

# docker-cli-compose provides the `docker compose` plugin the agent shells out to.
RUN apk add --no-cache docker-cli docker-cli-compose

WORKDIR /app
COPY agent.py .
EXPOSE 9000
ENTRYPOINT ["python", "-u", "/app/agent.py"]
