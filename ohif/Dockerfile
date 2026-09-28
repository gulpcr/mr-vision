# Keep in sync with the ohif image pinned in docker-compose.yml.
FROM ohif/app:v3.12.2

# Copy custom config that points to our Orthanc DICOMweb
COPY app-config.js /usr/share/nginx/html/app-config.js

EXPOSE 80
