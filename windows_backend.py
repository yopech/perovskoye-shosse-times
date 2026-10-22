"""Windows printing via SumatraPDF and per-user Task Scheduler tasks.

Printer names and file paths are arguments/environment values, never PowerShell code.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common import ROOT,STATE

PRINT_TASK='PerovskoyeTimes-Print'
AWAKE_TASK='PerovskoyeTimes-Awake'


def powershell(script, values=None, timeout=30):
    prefix="$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',prefix+script],
        capture_output=True,text=True,encoding='utf-8',timeout=timeout,
        env={**os.environ,**(values or {})},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise RuntimeError('Windows: '+result.stderr.strip()[:400])
    return result.stdout.strip()


def printers():
    return json.loads(powershell('ConvertTo-Json -InputObject @(Get-Printer | Select-Object -ExpandProperty Name) -Compress') or '[]')


def sumatra_path(cfg):
    candidates=[cfg.get('windows',{}).get('sumatra_path'),shutil.which('SumatraPDF.exe')]
    for env in ('LOCALAPPDATA','PROGRAMFILES','PROGRAMFILES(X86)'):
        if os.environ.get(env):
            candidates.append(str(Path(os.environ[env])/'SumatraPDF'/'SumatraPDF.exe'))
    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    raise FileNotFoundError('Установи SumatraPDF с sumatrapdfreader.org или укажи windows.sumatra_path в config.json')


def jobs(printer):
    output=powershell('ConvertTo-Json -InputObject @(Get-PrintJob -PrinterName $env:GAZETTE_PRINTER | '
        'Select-Object ID,DocumentName) -Compress',{'GAZETTE_PRINTER':printer})
    return json.loads(output or '[]')


def submit(cfg,pdf,day):
    executable=sumatra_path(cfg)
    spool=STATE/'print-spool'
    spool.mkdir(exist_ok=True)
    target=spool/f'perovskoye-times-{day}.pdf'
    shutil.copyfile(pdf,target)
    result=subprocess.run([executable,'-print-to',cfg['printer'],'-silent','-print-settings',
        'paper=A4,simplex,1x',str(target)],capture_output=True,text=True,timeout=90,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        return {'state':'uncertain_or_failed','backend':'windows',
            'detail':f'SumatraPDF: код {result.returncode}. Проверь очередь перед повтором.'}
    ledger={'state':'submitted','backend':'windows','document':target.name}
    try:
        matches=[j for j in jobs(cfg['printer']) if str(j['DocumentName']).casefold() in
            (target.name.casefold(),str(target).casefold())]
        if len(matches)==1:
            ledger['job_id']=str(matches[0]['ID'])
        elif len(matches)>1:
            ledger['detail']='В очереди несколько одноимённых заданий: требуется ручная проверка.'
    except (RuntimeError,OSError,subprocess.TimeoutExpired,json.JSONDecodeError):
        ledger['detail']='SumatraPDF принял PDF, но номер очереди не получен. При проблеме проверь очередь вручную.'
    return ledger


def cancel_pending(cfg,ledger):
    """Do not cancel a recycled queue ID or somebody else's document."""
    expected=ledger.get('document','')
    matching=[j for j in jobs(cfg['printer']) if str(j['ID'])==str(ledger['job_id']) and
        str(j['DocumentName']).casefold()==expected.casefold()]
    if not matching:
        return False
    powershell('Remove-PrintJob -PrinterName $env:GAZETTE_PRINTER -ID ([int]$env:GAZETTE_JOB)',
        {'GAZETTE_PRINTER':cfg['printer'],'GAZETTE_JOB':str(ledger['job_id'])})
    return True


def install_schedule():
    # Interactive, limited user tasks; no password, administrator rights or global policy change.
    script=r'''
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$printAction=New-ScheduledTaskAction -Execute $env:GAZETTE_PYTHON -Argument ('-X utf8 "'+$env:GAZETTE_SCRIPT+'" print-tick') -WorkingDirectory $env:GAZETTE_ROOT
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'PerovskoyeTimes-Print' -Action $printAction -Trigger $repeat -Principal $principal -Settings $settings -Force | Out-Null
$awakeAction=New-ScheduledTaskAction -Execute $env:GAZETTE_PYTHON -Argument ('-X utf8 "'+$env:GAZETTE_SCRIPT+'" keep-awake') -WorkingDirectory $env:GAZETTE_ROOT
$logon=New-ScheduledTaskTrigger -AtLogOn -User $user
$start=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$awakeSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'PerovskoyeTimes-Awake' -Action $awakeAction -Trigger @($logon,$start) -Principal $principal -Settings $awakeSettings -Force | Out-Null
Start-ScheduledTask -TaskName 'PerovskoyeTimes-Awake'
'''
    powershell(script,{'GAZETTE_PYTHON':sys.executable,'GAZETTE_SCRIPT':str(ROOT/'gazette.py'),
        'GAZETTE_ROOT':str(ROOT)})
    return {'installed':[PRINT_TASK,AWAKE_TASK],'platform':'windows'}


def uninstall_schedule():
    powershell("foreach($name in @('PerovskoyeTimes-Print','PerovskoyeTimes-Awake')) { "
        "if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { "
        "Stop-ScheduledTask -TaskName $name; Unregister-ScheduledTask -TaskName $name -Confirm:$false } }")


def keep_awake():
    class PowerStatus(ctypes.Structure):
        _fields_=[('ACLineStatus',ctypes.c_ubyte),('BatteryFlag',ctypes.c_ubyte),
            ('BatteryLifePercent',ctypes.c_ubyte),('SystemStatusFlag',ctypes.c_ubyte),
            ('BatteryLifeTime',ctypes.c_ulong),('BatteryFullLifeTime',ctypes.c_ulong)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.SetThreadExecutionState.argtypes=[ctypes.c_uint]
    kernel.SetThreadExecutionState.restype=ctypes.c_uint
    continuous,required=0x80000000,0x00000001
    try:
        while True:
            power=PowerStatus()
            on_ac=bool(kernel.GetSystemPowerStatus(ctypes.byref(power)) and power.ACLineStatus==1)
            kernel.SetThreadExecutionState(continuous | (required if on_ac else 0))
            time.sleep(30)
    finally:
        kernel.SetThreadExecutionState(continuous)
