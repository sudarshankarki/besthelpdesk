# Coturn Docker Hub Push And Linux Pull

This project builds the TURN image as `bestsupport-coturn:latest` from [docker/coturn/Dockerfile](e:\pythonproject\BESTSUPPORT\docker\coturn\Dockerfile).

## 1. Build the image on the current machine

```powershell
docker compose build coturn
```

## 2. Tag it for Docker Hub

Replace `YOUR_DOCKERHUB_USER` with your Docker Hub username or organization.

```powershell
docker tag bestsupport-coturn:latest YOUR_DOCKERHUB_USER/bestsupport-coturn:latest
docker tag bestsupport-coturn:latest YOUR_DOCKERHUB_USER/bestsupport-coturn:v1
```

## 3. Log in and push

```powershell
docker login
docker push YOUR_DOCKERHUB_USER/bestsupport-coturn:latest
docker push YOUR_DOCKERHUB_USER/bestsupport-coturn:v1
```

## 4. Copy the Linux deployment files

Copy these items to the Linux host:

- `docker/coturn/docker-compose.pull.yml`
- `docker/coturn/coturn.env.example`
- `docker/coturn/certs/`

Create a Linux-specific `coturn.env` beside `docker-compose.pull.yml`.
Do not reuse the app `.env`, because that file contains unrelated application settings and may differ from Linux.

You can start by copying `coturn.env.example` to `coturn.env`.

The Linux `coturn.env` file should include the TURN settings your server needs, for example:

```dotenv
TURN_REALM=helpdesk.bfcl.com
TURN_AUTH_SECRET=replace-with-your-secret
# For public or NATed Linux hosts, set the reachable host/public IP:
TURN_EXTERNAL_IP=203.0.113.10
TURN_PORT=3478
TURN_TLS_PORT=5349
TURN_MIN_PORT=49160
TURN_MAX_PORT=49200
```

Use either:

- `TURN_AUTH_SECRET` for temporary credentials, or
- `TURN_USERNAME` and `TURN_PASSWORD` for static credentials

## 5. Pull and start on Linux

From the directory that contains `docker-compose.pull.yml` and `coturn.env`, run:

```bash
export COTURN_IMAGE=YOUR_DOCKERHUB_USER/bestsupport-coturn:latest
docker compose -f docker-compose.pull.yml pull
docker compose -f docker-compose.pull.yml up -d
```

## 6. Verify on Linux

```bash
docker compose -f docker-compose.pull.yml ps
docker compose -f docker-compose.pull.yml logs --tail 50
ss -lntup | grep -E '3478|5349|49160|49200'
```

## Notes

- If the Linux host is accessed by IP or through NAT, set `TURN_EXTERNAL_IP`.
- The image itself does not contain your secret or certificates. Those stay on the Linux host in `coturn.env` and `certs/`.
- If you want the Linux host to always use a fixed version, pull `YOUR_DOCKERHUB_USER/bestsupport-coturn:v1` instead of `latest`.
