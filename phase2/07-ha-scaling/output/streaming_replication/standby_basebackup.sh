pg_basebackup -h primary -p 5432 -U replicator -D /var/lib/postgresql/standby -Fp -Xs -P -R -S standby_slot -C
