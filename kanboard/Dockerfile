# Kanboard with the MarcusDevEnv plugin pre-installed.
#
# Baking the plugin into the image means Railway (and any other
# container platform) gets the plugin automatically — no manual file
# copying or volume management required.
#
# The data directory (/var/www/app/data) is kept as a persistent volume
# so task data survives container restarts and redeploys.

FROM kanboard/kanboard:latest

# Install the MarcusDevEnv plugin that adds:
#   - Live AI-agent count badge on the board view
#   - "View Live Changes" dev-environment button on each task
#   - "Dependencies" panel on each task (which tickets block / are blocked by this one)
COPY plugins/MarcusDevEnv /var/www/app/plugins/MarcusDevEnv

# Kanboard runs as www-data; fix ownership so it can read plugin files
RUN chown -R www-data:www-data /var/www/app/plugins/MarcusDevEnv

# PHP-FPM env passthrough for SESSION_HANDLER/DB_WAL_MODE — the base
# image's own php-fpm.d/env.conf allowlist doesn't cover either
# variable, so setting them in docker-compose.yml's environment: block
# alone never reaches a real web request (only docker exec's shell and
# PHP-CLI, which is misleading during troubleshooting since both LOOK
# correct). See php-fpm-marcus-env.conf's own header comment for the
# full, empirically-confirmed explanation (verified against
# kanboard/kanboard:v1.2.53, which pins /etc/php84/php-fpm.d/ — this
# image tracks :latest instead, so the PHP version's config directory
# name isn't assumed; it's located at build time so this doesn't
# silently break on a PHP version bump). Baked in at build time (not
# bind-mounted) because this Dockerfile is what Railway/Fly.io build
# from directly — no host filesystem access at deploy time there.
COPY php-fpm-marcus-env.conf /tmp/marcus-env.conf
RUN set -eu; \
    fpm_d=$(find /etc -mindepth 2 -maxdepth 2 -type d -name 'php-fpm.d' | head -n1); \
    if [ -z "$fpm_d" ]; then \
        echo "ERROR: could not locate a php-fpm.d directory in this base image" >&2; \
        exit 1; \
    fi; \
    cp /tmp/marcus-env.conf "$fpm_d/marcus-env.conf"; \
    rm /tmp/marcus-env.conf

# Expose Kanboard's HTTP port
EXPOSE 80
