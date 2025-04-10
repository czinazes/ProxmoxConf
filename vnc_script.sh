#!/usr/bin/env bash

source /opt/vnc-venv/bin/activate

VS="127.0.0.1:77"

vncdo -s "$VS" move 770 780 sleep 0.1 click 1 sleep 0.1 move 375 600 click 1 sleep 0.3 click 1 sleep 0.1 
vncdo -s "$VS" move 520 300 sleep 0.1 mousedown 1 sleep 0.1 drag 300 300 sleep 0.1 mouseup 1 sleep 0.1 
vncdo -s "$VS" move 650 400 sleep 0.1 click 1 sleep 0.1 move 320 330 sleep 0.1 click 1 sleep 0.3 click 1
