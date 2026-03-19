FROM coturn/coturn:4.9-alpine

COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 3478 3478/udp 5349 5349/udp 49160-49200/udp

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
