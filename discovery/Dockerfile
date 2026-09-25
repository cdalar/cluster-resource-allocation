# Image for the in-cluster collector + dashboard (server.py). Build context: discovery/
FROM alpine:3.22 AS kubectl
ARG KUBECTL_VERSION=v1.37.1
ARG TARGETARCH=amd64
RUN apk add --no-cache curl \
 && curl -fsSLo /kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256")  /kubectl" | sha256sum -c - \
 && chmod 0755 /kubectl

FROM python:3.13-slim
COPY --from=kubectl /kubectl /usr/local/bin/kubectl
WORKDIR /app
COPY discover.py server.py ./
COPY static/ ./static/
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python3", "/app/server.py"]
CMD ["--data-dir", "/data"]
