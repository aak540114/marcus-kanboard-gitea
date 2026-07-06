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

# Expose Kanboard's HTTP port
EXPOSE 80
