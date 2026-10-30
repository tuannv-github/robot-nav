cmake_minimum_required(VERSION 3.5)
project(g1_controller)

find_package(ament_cmake REQUIRED)

install(PROGRAMS
  scripts/robot_bridge.py
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY
  launch
  console
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
