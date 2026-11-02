ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq python3 sqlite3 procps iproute2 iputils-ping \
 && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
