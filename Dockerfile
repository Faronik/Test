FROM debian:bookworm-slim AS toolchain
RUN apt-get update && apt-get install -y --no-install-recommends g++ cmake make pkg-config libpq-dev libssl-dev libsqlite3-dev nlohmann-json3-dev libcpp-httplib-dev ca-certificates && rm -rf /var/lib/apt/lists/*
FROM toolchain AS build
WORKDIR /source
COPY CMakeLists.txt ./
COPY src ./src
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel 2
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 libssl3 libsqlite3-0 libcpp-httplib0.11 ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --system --uid 10001 toolstock && mkdir -p /var/lib/toolstock/photos && chown -R toolstock /var/lib/toolstock
WORKDIR /app
COPY --from=build /source/build/toolstock /usr/local/bin/toolstock
COPY --chown=toolstock:toolstock web ./web
COPY --chown=toolstock:toolstock sql ./sql
COPY --chown=toolstock:toolstock docs ./docs
USER toolstock
ENV TOOLSTOCK_DATA=/var/lib/toolstock TOOLSTOCK_WEB=/app/web TOOLSTOCK_SQL=/app/sql TOOLSTOCK_DOCS=/app/docs
EXPOSE 8000
CMD ["toolstock"]
