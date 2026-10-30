FROM ros:humble-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-numpy \
      python3-zmq \
      ros-humble-nav-msgs \
      ros-humble-rmw-cyclonedds-cpp \
      ros-humble-sensor-msgs \
      ros-humble-tf2-ros \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY control_bridge.py entrypoint.sh fastdds.xml ./
RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/entrypoint.sh"]
