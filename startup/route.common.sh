#!/bin/bash
# Shared host routes for WiFi / 5G profiles (sourced by route.wifi.sh and route.5g.sh).

IP_WIFI_GATEWAY=10.1.100.1
IP_5G_GATEWAY=192.168.1.1

IP_VIDEO_MQTT_BROKER=10.1.106.210
IP_LLM_SERVER=10.1.110.57
IP_LOCATION_SERVER=10.1.101.216

ROUTE_ALL_DESTINATIONS=(
    "$IP_VIDEO_MQTT_BROKER"
    "$IP_LLM_SERVER"
    "$IP_LOCATION_SERVER"
    10.1.101.211
    10.1.101.220
)

route_delete_all() {
    local dest
    for dest in "${ROUTE_ALL_DESTINATIONS[@]}"; do
        sudo ip route del "${dest}/32" 2>/dev/null || true
    done
}

route_add_host() {
    local dest=$1
    local gateway=$2
    sudo ip route add "${dest}/32" via "$gateway"
}
