# Egress proxy: the only route to the internet for api/worker. Allows CONNECT to allowlisted hosts only.
FROM alpine:3.21
RUN apk add --no-cache tinyproxy
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
