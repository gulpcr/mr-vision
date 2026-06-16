FROM orthancteam/orthanc:24.6.2

# Enable built-in plugins via environment variables
ENV STONE_WEB_VIEWER_PLUGIN_ENABLED=true
ENV DICOM_WEB_PLUGIN_ENABLED=true

COPY orthanc.json /etc/orthanc/orthanc.json
COPY on_stable_study.lua /etc/orthanc/on_stable_study.lua

EXPOSE 4242 8042
