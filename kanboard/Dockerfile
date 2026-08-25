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
# correct). See kanboard/php-fpm-env.conf's own header comment (used by
# the root docker-compose.yml's bind mount) for the full,
# empirically-confirmed explanation.
#
# Two mechanisms were tried and rejected before this one: (1) a
# separate sibling conf file — silently never read, since this image's
# pool conf loads env.conf by its EXACT filename
# (`include = .../php-fpm.d/env.conf`), not a directory-wide glob;
# confirmed live, this is why the first version of this fix didn't
# work despite the file being correctly present on disk. (2) a SECOND
# `include =` line pointing at that sibling file — PHP-FPM's own
# config parser (sapi/fpm/fpm/fpm_conf.c) rejects a second `include`
# directive within one file outright ("two includes at the same
# time!"), which would have broken Kanboard's startup entirely rather
# than just failing to forward two env vars. APPENDING these two lines
# directly onto whatever env.conf the build finds — the SAME file
# already trusted by the existing single include — sidesteps both
# problems. Located dynamically rather than hardcoded to
# /etc/php84/..., since this image tracks :latest (unlike the pinned
# v1.2.53 the live diagnosis above was run against), so the PHP
# version's directory name isn't assumed.
RUN set -eu; \
    env_conf=$(find /etc -type f -name 'env.conf' -path '*php-fpm*' | head -n1); \
    if [ -z "$env_conf" ]; then \
        echo "ERROR: could not locate php-fpm.d/env.conf in this base image — cannot forward SESSION_HANDLER/DB_WAL_MODE to PHP-FPM" >&2; \
        exit 1; \
    fi; \
    printf '\n; Marcus additions — see kanboard/php-fpm-env.conf for why\nenv[SESSION_HANDLER] = $SESSION_HANDLER\nenv[DB_WAL_MODE] = $DB_WAL_MODE\n' >> "$env_conf"

# Expose Kanboard's HTTP port
EXPOSE 80
