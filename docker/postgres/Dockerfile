FROM paradedb/paradedb@sha256:9b15fc383bcd771f1e32e222bd1603c4c29856c481f6bd06f1960e89608c02f0 AS vector
FROM postgres:18.6@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722
COPY --from=vector /usr/lib/postgresql/18/lib/vector.so /usr/lib/postgresql/18/lib/
COPY --from=vector /usr/share/postgresql/18/extension/vector* /usr/share/postgresql/18/extension/
ADD --checksum=sha256:465fe95726b4f26d354cadeea1ba983441f239ad75013bd54038dabdf2677101 https://github.com/timescale/pg_textsearch/releases/download/v1.5.1/pg_textsearch-pg18-v1.5.1.tar.gz /tmp/textsearch.tar.gz
RUN tar xzf /tmp/textsearch.tar.gz -C /tmp && cp /tmp/pg18/*.so /usr/lib/postgresql/18/lib/ && cp /tmp/pg18/*.sql /tmp/pg18/*.control /usr/share/postgresql/18/extension/ && rm -rf /tmp/pg18 /tmp/textsearch.tar.gz
CMD ["postgres", "-c", "shared_preload_libraries=pg_textsearch", "-c", "pg_textsearch.memory_limit=128MB", "-c", "hot_standby_feedback=on"]
