@echo off
REM Volvo Fleet Assistant - type `fleet` to start chatting.
powershell -ExecutionPolicy Bypass -File "%~dp0fleet.ps1" %*
