#!/bin/sh
set -eu

TURN_PORT="${TURN_PORT:-3478}"
TURN_TLS_PORT="${TURN_TLS_PORT:-5349}"
TURN_MIN_PORT="${TURN_MIN_PORT:-49160}"
TURN_MAX_PORT="${TURN_MAX_PORT:-49200}"
TURN_REALM="${TURN_REALM:-helpdesk.fcl.com}"
TURN_AUTH_SECRET="${TURN_AUTH_SECRET:-${WEBRTC_TURN_AUTH_SECRET:-}}"
TURN_USERNAME="${TURN_USERNAME:-${WEBRTC_TURN_USERNAME:-}}"
TURN_PASSWORD="${TURN_PASSWORD:-${WEBRTC_TURN_PASSWORD:-}}"
TURN_EXTERNAL_IP="${TURN_EXTERNAL_IP:-}"
TURN_CERT_FILE="${TURN_CERT_FILE:-/certs/helpdesk.crt}"
TURN_KEY_FILE="${TURN_KEY_FILE:-/certs/helpdesk.key}"

set -- \
    --listening-port="${TURN_PORT}" \
    --tls-listening-port="${TURN_TLS_PORT}" \
    --min-port="${TURN_MIN_PORT}" \
    --max-port="${TURN_MAX_PORT}" \
    --realm="${TURN_REALM}" \
    --user="${TURN_USERNAME}:${TURN_PASSWORD}" \
    --lt-cred-mech \
    --fingerprint \
    --stale-nonce \
    --no-cli \
    --no-multicast-peers \
    --log-file=stdout \
    --simple-log

if [ -n "${TURN_AUTH_SECRET}" ]; then
    set -- "$@" \
        --lt-cred-mech \
        --use-auth-secret \
        --static-auth-secret="${TURN_AUTH_SECRET}"
elif [ -n "${TURN_USERNAME}" ] && [ -n "${TURN_PASSWORD}" ]; then
    set -- "$@" \
        --user="${TURN_USERNAME}:${TURN_PASSWORD}" \
        --lt-cred-mech
else
    echo "Set TURN_AUTH_SECRET for temporary credentials, or TURN_USERNAME and TURN_PASSWORD for static credentials." >&2
    exit 1
fi

if [ -n "${TURN_EXTERNAL_IP}" ]; then
    set -- "$@" --external-ip="${TURN_EXTERNAL_IP}"
else
    echo "TURN_EXTERNAL_IP is not set. TURN relay may fail behind NAT." >&2
fi

if [ -r "${TURN_CERT_FILE}" ] && [ -r "${TURN_KEY_FILE}" ]; then
    set -- "$@" --cert="${TURN_CERT_FILE}" --pkey="${TURN_KEY_FILE}"
else
    set -- "$@" --no-tls --no-dtls
fi

exec turnserver "$@"
