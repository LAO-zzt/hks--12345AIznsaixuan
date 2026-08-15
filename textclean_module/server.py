"""12345工单清洗模块 Web 界面。

功能：
- 文件上传（带实时进度条）
- Job管理（创建/运行/查看进度）
- Batch列表与状态
- 清洗结果查看
- 重复工单识别
- 数据质量统计

启动：
    python server.py            # 或 python -m textclean_module.server
访问：
    http://127.0.0.1:5000
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, Optional

from flask import Flask, jsonify, render_template_string, request

# 配置日志输出到控制台
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 保证导入本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------- 模块路径（独立化：所有数据落在模块 data/ 目录） ----------
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(MODULE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "cleaner.db")

from ticket_cleaner.batch_engine import BatchEngine, ProgressInfo
from ticket_cleaner.config import Config
from ticket_cleaner.duplicate import DuplicateDetector
from ticket_cleaner.reader import ExcelReader
from ticket_cleaner.storage import Storage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB

# 上传目录（落在模块 data/ 下）
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 全局状态
_state: Dict[str, Any] = {
    "upload_progress": {},   # upload_id -> {filename, received, total, status}
    "active_jobs": {},      # job_id -> latest ProgressInfo
    "job_threads": {},      # job_id -> thread
    "job_engines": {},      # job_id -> BatchEngine 实例（用于停止控制）
}
_state_lock = threading.Lock()


def _cfg_with_source(excel_path: str, batch_size: int = 200) -> Config:
    """构造配置，指定数据源。"""
    return Config(
        source_excel_path=excel_path,
        batch_size=batch_size,
        db_path=DB_PATH,
    )


# ============ 页面 ============

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>12345工单清洗平台</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; }
.header { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; padding: 18px 30px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }
.header h1 { font-size: 22px; margin-bottom: 4px; }
.header .sub { font-size: 13px; opacity: .9; }
.container { max-width: 1400px; margin: 20px auto; padding: 0 20px; }
.card { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.card h2 { font-size: 16px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; color: #444; }
.row { display: flex; gap: 16px; flex-wrap: wrap; }
.col { flex: 1; min-width: 280px; }
label { display: block; font-size: 13px; color: #666; margin-bottom: 6px; }
input[type=text], input[type=number], select { width: 100%; padding: 8px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
input[type=file] { display: none; }
.btn { display: inline-block; padding: 8px 18px; background: #667eea; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; transition: all .2s; }
.btn:hover { background: #5568d3; transform: translateY(-1px); }
.btn-sec { background: #6c757d; }
.btn-sec:hover { background: #5a6268; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn:disabled { background: #aaa; cursor: not-allowed; transform: none; }
.upload-area { border: 2px dashed #ccc; border-radius: 8px; padding: 30px; text-align: center; cursor: pointer; transition: all .2s; }
.upload-area:hover, .upload-area.dragover { border-color: #667eea; background: #f8f9ff; }
.upload-area p { color: #888; margin-top: 8px; font-size: 13px; }
.progress-wrap { margin-top: 14px; display: none; }
.progress-bar { height: 22px; background: #e9ecef; border-radius: 11px; overflow: hidden; position: relative; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #667eea, #764ba2); width: 0%; transition: width .2s; }
.progress-text { position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 12px; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat-item { background: #f8f9fa; padding: 14px; border-radius: 8px; border-left: 3px solid #667eea; }
.stat-item .label { font-size: 12px; color: #888; }
.stat-item .value { font-size: 20px; font-weight: 600; color: #333; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #f0f0f0; }
th { background: #f8f9fa; font-weight: 600; color: #555; }
tr:hover { background: #fafbfc; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.tag-success { background: #d4edda; color: #155724; }
.tag-running { background: #fff3cd; color: #856404; }
.tag-failed { background: #f8d7da; color: #721c24; }
.tag-pending { background: #e2e3e5; color: #383d41; }
.tag-partial { background: #ffeaa7; color: #6c5b1c; }
.content-cell { max-width: 400px; max-height: 100px; overflow: auto; white-space: pre-wrap; word-break: break-all; color: #555; }
.muted { color: #999; }
.section-tabs { display: flex; gap: 4px; border-bottom: 1px solid #e0e0e0; margin-bottom: 16px; }
.tab { padding: 10px 18px; cursor: pointer; border-bottom: 2px solid transparent; color: #888; font-size: 14px; }
.tab.active { color: #667eea; border-bottom-color: #667eea; font-weight: 600; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; background: #333; color: #fff; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 9999; }
.toast.show { opacity: 1; }
.toast.err { background: #dc3545; }
.toast.ok { background: #28a745; }
.pager { margin-top: 12px; text-align: center; }
.pager button { margin: 0 4px; }
.empty { text-align: center; padding: 30px; color: #999; font-size: 14px; }
</style>
</head>
<body>
<div class="header">
    <h1>12345热线工单数据清洗与AI标准化平台</h1>
    <div class="sub">原始工单 → 字段清洗 → 实体抽取 → 归一化 → Semantic Content → Embedding → 重复识别</div>
    <div style="margin-top:10px;">
        <a href="/" class="btn btn-sm" style="background:rgba(255,255,255,.2);">清洗管理</a>
        <a href="/data" class="btn btn-sm" style="background:rgba(255,255,255,.2);">数据浏览</a>
    </div>
</div>

<div class="container">

<!-- 上传 -->
<div class="card">
    <h2>1. 数据导入</h2>
    <div class="row">
        <div class="col">
            <label>选择 Excel 文件 (.xlsx)</label>
            <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
                <div style="font-size:36px;">📁</div>
                <p>点击或拖拽文件到这里上传</p>
                <p class="muted" style="font-size:12px;">支持 .xlsx 格式，单文件最大 500MB</p>
            </div>
            <input type="file" id="fileInput" accept=".xlsx,.xls">
        </div>
        <div class="col">
            <label>当前数据源</label>
            <div id="currentSource" style="padding:10px;background:#f8f9fa;border-radius:6px;font-size:13px;margin-bottom:12px;">
                <span class="muted">未选择</span>
            </div>
            <label>批次大小</label>
            <select id="batchSize">
                <option value="200">200 条/批（低配）</option>
                <option value="500" selected>500 条/批</option>
                <option value="1000">1000 条/批（默认）</option>
                <option value="2000">2000 条/批（高性能）</option>
            </select>
        </div>
    </div>
    <div class="progress-wrap" id="progressWrap">
        <label>上传进度</label>
        <div class="progress-bar">
            <div class="progress-fill" id="progressFill"></div>
            <div class="progress-text" id="progressText">0%</div>
        </div>
        <div id="uploadInfo" style="margin-top:6px;font-size:12px;color:#888;"></div>
    </div>
</div>

<!-- Tabs -->
<div class="card">
    <div class="section-tabs">
        <div class="tab active" data-tab="jobs">任务管理</div>
        <div class="tab" data-tab="batches">批次详情</div>
        <div class="tab" data-tab="results">清洗结果</div>
        <div class="tab" data-tab="duplicates">重复识别</div>
        <div class="tab" data-tab="stats">数据质量</div>
    </div>

    <!-- Jobs -->
    <div class="tab-content active" id="tab-jobs">
        <h2>2. 清洗任务</h2>
        <div class="row" style="margin-bottom:14px;">
            <div class="col">
                <label>Job ID（留空自动生成）</label>
                <input type="text" id="jobId" placeholder="如 job-20250101">
            </div>
            <div class="col" style="display:flex;align-items:flex-end;gap:8px;">
                <button class="btn" id="createJobBtn" onclick="createJob()">创建任务</button>
                <button class="btn btn-sec" onclick="runJob()">运行全部</button>
                <button class="btn btn-sec" onclick="loadJobs()">刷新</button>
            </div>
        </div>
        <div id="jobsList"></div>
    </div>

    <!-- Batches -->
    <div class="tab-content" id="tab-batches">
        <h2>批次列表</h2>
        <div class="row" style="margin-bottom:14px;align-items:flex-end;">
            <div class="col">
                <label>Job ID</label>
                <input type="text" id="batchJobId" placeholder="输入Job ID">
            </div>
            <div class="col" style="display:flex;gap:8px;">
                <button class="btn btn-sm" onclick="loadBatches()">查询</button>
                <button class="btn btn-sm btn-sec" onclick="loadBatches('FAILED')">仅失败</button>
            </div>
        </div>
        <div id="batchesList"></div>
    </div>

    <!-- Results -->
    <div class="tab-content" id="tab-results">
        <h2>清洗结果</h2>
        <div class="row" style="margin-bottom:14px;align-items:flex-end;">
            <div class="col">
                <label>Job ID</label>
                <input type="text" id="resultJobId" placeholder="输入Job ID">
            </div>
            <div class="col">
                <label>每页条数</label>
                <select id="resultPageSize">
                    <option value="10">10</option>
                    <option value="20" selected>20</option>
                    <option value="50">50</option>
                    <option value="100">100</option>
                </select>
            </div>
            <div class="col" style="display:flex;gap:8px;">
                <button class="btn btn-sm" onclick="loadResults(0)">查询</button>
            </div>
        </div>
        <div id="resultsList"></div>
        <div class="pager" id="resultPager"></div>
    </div>

    <!-- Duplicates -->
    <div class="tab-content" id="tab-duplicates">
        <h2>重复工单识别</h2>
        <div class="row" style="margin-bottom:14px;align-items:flex-end;">
            <div class="col">
                <label>Job ID</label>
                <input type="text" id="dupJobId" placeholder="输入Job ID">
            </div>
            <div class="col">
                <label>Top K（每工单对比数）</label>
                <input type="number" id="dupTopK" value="20" min="5" max="200">
            </div>
            <div class="col" style="display:flex;gap:8px;">
                <button class="btn btn-sm" onclick="findDuplicates()">查找重复</button>
            </div>
        </div>
        <div id="dupList"></div>
    </div>

    <!-- Stats -->
    <div class="tab-content" id="tab-stats">
        <h2>数据质量统计</h2>
        <div class="row" style="margin-bottom:14px;align-items:flex-end;">
            <div class="col">
                <label>Job ID</label>
                <input type="text" id="statsJobId" placeholder="输入Job ID">
            </div>
            <div class="col" style="display:flex;gap:8px;">
                <button class="btn btn-sm" onclick="loadStats()">统计</button>
            </div>
        </div>
        <div id="statsContent"></div>
    </div>
</div>

</div>

<div class="toast" id="toast"></div>

<script>
// ============ 通用 ============
function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || '');
    setTimeout(() => t.className = 'toast ' + (type || ''), 2500);
}

document.querySelectorAll('.tab').forEach(tab => {
    tab.onclick = () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    };
});

function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
    return (n/1024/1024).toFixed(2) + ' MB';
}

function statusTag(s) {
    const m = {SUCCESS:'tag-success', RUNNING:'tag-running', FAILED:'tag-failed',
               PENDING:'tag-pending', PARTIAL_SUCCESS:'tag-partial'};
    return '<span class="tag ' + (m[s]||'tag-pending') + '">' + s + '</span>';
}

// ============ 上传 ============
const fileInput = document.getElementById('fileInput');
const uploadArea = document.getElementById('uploadArea');
const progressWrap = document.getElementById('progressWrap');
const progressFill = document.getElementById('progressFill');
const progressText = document.getElementById('progressText');
const uploadInfo = document.getElementById('uploadInfo');

uploadArea.addEventListener('dragover', e => { e.preventDefault(); uploadArea.classList.add('dragover'); });
uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
uploadArea.addEventListener('drop', e => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; uploadFile(); }
});

fileInput.onchange = () => { if (fileInput.files.length) uploadFile(); };

function uploadFile() {
    const file = fileInput.files[0];
    if (!file) return;
    if (!file.name.match(/\\.xlsx$/i)) { toast('请上传 .xlsx 文件', 'err'); return; }

    const uploadId = 'u_' + Date.now();
    progressWrap.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = '0%';
    uploadInfo.textContent = file.name + ' (' + fmtSize(file.size) + ')';

    const fd = new FormData();
    fd.append('file', file);
    fd.append('upload_id', uploadId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');

    xhr.upload.onprogress = e => {
        if (e.lengthComputable) {
            const pct = Math.round(e.loaded / e.total * 100);
            progressFill.style.width = pct + '%';
            progressText.textContent = pct + '%';
            uploadInfo.textContent = file.name + ' · 上传中 ' + fmtSize(e.loaded) + ' / ' + fmtSize(e.total);
        }
    };

    xhr.onload = () => {
        if (xhr.status === 200) {
            const r = JSON.parse(xhr.responseText);
            if (r.ok) {
                toast('上传成功：' + r.filename, 'ok');
                uploadInfo.innerHTML = '<strong>✓ ' + r.filename + '</strong> · 共 ' + r.records + ' 条记录';
                document.getElementById('currentSource').innerHTML =
                    '<strong>' + r.filename + '</strong><br><span class="muted">' + r.path + '</span><br><span class="muted">记录数：' + r.records + '</span>';
                // 记录到后端
                fetch('/api/set_source', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({path: r.path, filename: r.filename, records: r.records})});
            } else {
                toast('上传失败：' + r.error, 'err');
            }
        } else {
            toast('上传失败 HTTP ' + xhr.status, 'err');
        }
    };

    xhr.onerror = () => toast('网络错误', 'err');
    xhr.send(fd);
}

// 初始加载当前数据源
fetch('/api/current_source').then(r=>r.json()).then(r => {
    if (r.path) {
        document.getElementById('currentSource').innerHTML =
            '<strong>' + r.filename + '</strong><br><span class="muted">' + r.path + '</span><br><span class="muted">记录数：' + r.records + '</span>';
    }
});

// ============ Jobs ============
function createJob() {
    const jobId = document.getElementById('jobId').value.trim() || ('job-' + Date.now());
    const batchSize = parseInt(document.getElementById('batchSize').value);
    fetch('/api/create_job', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: jobId, batch_size: batchSize})})
    .then(r=>r.json()).then(r => {
        if (r.ok) { toast('任务已创建：' + r.job_id + '，共 ' + r.total_batches + ' 批', 'ok'); loadJobs(); }
        else toast('创建失败：' + r.error, 'err');
    });
}

function runJob() {
    const jobId = document.getElementById('jobId').value.trim();
    if (!jobId) { toast('请先创建或填写Job ID', 'err'); return; }
    fetch('/api/run_job', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: jobId})})
    .then(r=>r.json()).then(r => {
        if (r.ok) toast('任务已启动，后台运行中', 'ok');
        else toast('启动失败：' + r.error, 'err');
    });
}

function loadJobs() {
    fetch('/api/jobs').then(r=>r.json()).then(r => {
        const el = document.getElementById('jobsList');
        if (!r.jobs || !r.jobs.length) { el.innerHTML = '<div class="empty">暂无任务</div>'; return; }
        let html = '<table><thead><tr><th>Job ID</th><th>状态</th><th>进度</th><th>批次大小</th><th>总批次</th><th>已完成</th><th>失败</th><th>创建时间</th><th>操作</th></tr></thead><tbody>';
        r.jobs.forEach(j => {
            const pct = j.total_batches ? (j.completed_batches / j.total_batches * 100).toFixed(1) : 0;
            const isRunning = j.status === 'RUNNING';
            const stopBtn = isRunning 
                ? '<button class="btn btn-sm" style="background:#dc3545;" onclick="stopJob(\\''+j.id+'\\')">停止</button> '
                : '';
            html += '<tr><td>' + j.id + '</td><td>' + statusTag(j.status) + '</td>'
                + '<td><div style="background:#e9ecef;border-radius:8px;height:16px;min-width:120px;overflow:hidden;"><div style="background:#667eea;height:100%;width:' + pct + '%;"></div></div><span style="font-size:11px;">' + pct + '%</span></td>'
                + '<td>' + j.batch_size + '</td><td>' + j.total_batches + '</td><td>' + j.completed_batches + '</td><td>' + j.failed_batches + '</td>'
                + '<td>' + (j.created_at||'') + '</td>'
                + '<td>' + stopBtn
                + '<button class="btn btn-sm" onclick="runJobById(\\''+j.id+'\\')">运行</button> '
                + '<button class="btn btn-sm btn-sec" onclick="setJobId(\\''+j.id+'\\')">填充</button></td></tr>';
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    });
}

function runJobById(id) {
    fetch('/api/run_job', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: id})})
    .then(r=>r.json()).then(r => r.ok ? toast('任务已启动', 'ok') : toast('失败：'+r.error, 'err'));
}

function stopJob(id) {
    if (!confirm('确定要停止任务 ' + id + ' 吗？')) return;
    fetch('/api/stop_job', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: id})})
    .then(r=>r.json()).then(r => r.ok ? toast('已发送停止请求', 'ok') : toast('停止失败：'+r.error, 'err'));
}

function setJobId(id) {
    document.getElementById('jobId').value = id;
    document.getElementById('batchJobId').value = id;
    document.getElementById('resultJobId').value = id;
    document.getElementById('dupJobId').value = id;
    document.getElementById('statsJobId').value = id;
}

// ============ Batches ============
function loadBatches(filterStatus) {
    const jobId = document.getElementById('batchJobId').value.trim();
    if (!jobId) { toast('请填写Job ID', 'err'); return; }
    let url = '/api/batches?job_id=' + encodeURIComponent(jobId);
    if (filterStatus) url += '&status=' + filterStatus;
    fetch(url).then(r=>r.json()).then(r => {
        const el = document.getElementById('batchesList');
        if (!r.batches || !r.batches.length) { el.innerHTML = '<div class="empty">无批次记录</div>'; return; }
        let html = '<table><thead><tr><th>批次号</th><th>状态</th><th>范围</th><th>记录数</th><th>成功</th><th>失败</th><th>开始时间</th><th>结束时间</th><th>操作</th></tr></thead><tbody>';
        r.batches.forEach(b => {
            html += '<tr><td>#' + b.batch_no + '</td><td>' + statusTag(b.status) + '</td>'
                + '<td>' + b.start_index + '-' + b.end_index + '</td><td>' + b.record_count + '</td>'
                + '<td>' + b.success_count + '</td><td>' + b.error_count + '</td>'
                + '<td>' + (b.started_at||'') + '</td><td>' + (b.finished_at||'') + '</td>'
                + '<td>' + (b.status==='FAILED' ? '<button class="btn btn-sm" onclick="retryBatch(\\''+jobId+'\\','+b.batch_no+')">重试</button>' : '') + '</td></tr>';
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    });
}

function retryBatch(jobId, batchNo) {
    fetch('/api/retry_batch', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: jobId, batch_no: batchNo})})
    .then(r=>r.json()).then(r => r.ok ? toast('已重试批次 ' + batchNo, 'ok') : toast('失败：'+r.error, 'err'));
}

// ============ Results ============
let resultPage = 0;
function loadResults(page) {
    const jobId = document.getElementById('resultJobId').value.trim();
    if (!jobId) { toast('请填写Job ID', 'err'); return; }
    resultPage = page || 0;
    const size = parseInt(document.getElementById('resultPageSize').value);
    fetch('/api/results?job_id=' + encodeURIComponent(jobId) + '&page=' + resultPage + '&size=' + size)
    .then(r=>r.json()).then(r => {
        const el = document.getElementById('resultsList');
        if (!r.data || !r.data.length) { el.innerHTML = '<div class="empty">无结果</div>'; document.getElementById('resultPager').innerHTML=''; return; }
        let html = '<table><thead><tr><th>工单号</th><th>原始内容</th><th>清洗内容</th><th>语义内容</th><th>主体</th><th>地点</th><th>事件</th><th>诉求</th><th>时间</th><th>质量分</th><th>可用</th></tr></thead><tbody>';
        r.data.forEach(d => {
            html += '<tr><td>' + d.ticket_no + '</td>'
                + '<td class="content-cell">' + (d.raw_content||'').substring(0,100) + (d.raw_content&&d.raw_content.length>100?'...':'') + '</td>'
                + '<td class="content-cell">' + (d.clean_content||'').substring(0,100) + '</td>'
                + '<td class="content-cell" style="color:#667eea;font-weight:500;">' + (d.semantic_content||'') + '</td>'
                + '<td>' + (d.organization_normalized||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.address_normalized||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.event_type||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.request||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.time_start||d.time_pattern||'') + '</td>'
                + '<td>' + (d.data_quality_score||0).toFixed(2) + '</td>'
                + '<td>' + (d.is_usable_for_duplicate?'<span class="tag tag-success">是</span>':'<span class="tag tag-failed">否</span>') + '</td></tr>';
        });
        html += '</tbody></table>';
        el.innerHTML = html;

        // pager
        const total = r.total;
        const totalPages = Math.ceil(total / size);
        let p = '<button class="btn btn-sm btn-sec" onclick="loadResults(' + Math.max(0,resultPage-1) + ')">上一页</button> ';
        p += '<span>第 ' + (resultPage+1) + ' / ' + totalPages + ' 页 · 共 ' + total + ' 条</span> ';
        p += '<button class="btn btn-sm btn-sec" onclick="loadResults(' + Math.min(totalPages-1,resultPage+1) + ')">下一页</button>';
        document.getElementById('resultPager').innerHTML = p;
    });
}

// ============ Duplicates ============
function findDuplicates() {
    const jobId = document.getElementById('dupJobId').value.trim();
    if (!jobId) { toast('请填写Job ID', 'err'); return; }
    const topK = parseInt(document.getElementById('dupTopK').value) || 20;
    const el = document.getElementById('dupList');
    el.innerHTML = '<div class="empty">查找中...（可能耗时较长）</div>';
    fetch('/api/duplicates', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: jobId, top_k: topK})})
    .then(r=>r.json()).then(r => {
        if (!r.ok) { el.innerHTML = '<div class="empty">' + r.error + '</div>'; return; }
        const cands = r.candidates || [];
        if (!cands.length) { el.innerHTML = '<div class="empty">未找到重复候选</div>'; return; }
        let html = '<p style="margin-bottom:10px;">找到 <strong>' + cands.length + '</strong> 个候选对，其中 <strong style="color:#28a745;">' + cands.filter(c=>c.duplicate).length + '</strong> 判定为重复，<strong style="color:#ffc107;">' + cands.filter(c=>!c.duplicate).length + '</strong> 相似但非重复</p>';
        html += '<table><thead><tr><th>#</th><th>工单A</th><th>工单B</th><th>相似度</th><th>判定</th><th>特征得分</th><th>原因</th></tr></thead><tbody>';
        cands.forEach((c,i) => {
            html += '<tr><td>' + (i+1) + '</td><td>' + c.ticket_no_a + '</td><td>' + c.ticket_no_b + '</td>'
                + '<td>' + (c.similarity*100).toFixed(1) + '%</td>'
                + '<td>' + (c.duplicate ? '<span class="tag tag-success">重复</span>' : '<span class="tag tag-partial">相似</span>') + '</td>'
                + '<td>' + (c.details.feature_score||0).toFixed(2) + '</td>'
                + '<td style="font-size:12px;">' + (c.reason||'') + '</td></tr>';
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    });
}

// ============ Stats ============
function loadStats() {
    const jobId = document.getElementById('statsJobId').value.trim();
    if (!jobId) { toast('请填写Job ID', 'err'); return; }
    fetch('/api/stats?job_id=' + encodeURIComponent(jobId))
    .then(r=>r.json()).then(r => {
        const el = document.getElementById('statsContent');
        if (!r.job) { el.innerHTML = '<div class="empty">未找到任务</div>'; return; }
        let html = '<div class="stats">';
        html += statItem('总工单', r.job.total_records);
        html += statItem('已清洗', r.total_cleaned);
        html += statItem('清洗失败', r.failed);
        html += statItem('可用于重复判断', r.usable_for_duplicate);
        html += statItem('主体识别率', (r.org_recognition_rate*100).toFixed(1) + '%');
        html += statItem('地点识别率', (r.addr_recognition_rate*100).toFixed(1) + '%');
        html += statItem('事件识别率', (r.event_recognition_rate*100).toFixed(1) + '%');
        html += statItem('诉求识别率', (r.request_recognition_rate*100).toFixed(1) + '%');
        html += '</div>';
        html += '<div style="margin-top:14px;"><strong>任务信息：</strong> 状态 ' + statusTag(r.job.status) + ' · 已完成批次 ' + r.job.completed_batches + '/' + r.job.total_batches + ' · 失败批次 ' + r.job.failed_batches + '</div>';
        el.innerHTML = html;
    });
}

function statItem(label, value) {
    return '<div class="stat-item"><div class="label">' + label + '</div><div class="value">' + value + '</div></div>';
}

// 初始加载
loadJobs();
setInterval(() => {
    // 自动刷新任务列表
    if (document.querySelector('.tab.active').dataset.tab === 'jobs') loadJobs();
}, 3000);
</script>
</body>
</html>
"""


# ============ 路由 ============

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


# ============ 数据浏览页面 ============

DATA_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>工单数据浏览 - 12345清洗平台</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; }
.header { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; padding: 18px 30px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }
.header h1 { font-size: 22px; margin-bottom: 4px; }
.header .sub { font-size: 13px; opacity: .9; }
.container { max-width: 1600px; margin: 20px auto; padding: 0 20px; }
.card { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.card h2 { font-size: 16px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; color: #444; }
.row { display: flex; gap: 16px; flex-wrap: wrap; }
.col { flex: 1; min-width: 220px; }
label { display: block; font-size: 13px; color: #666; margin-bottom: 6px; }
input[type=text], input[type=number], select { width: 100%; padding: 8px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
.btn { display: inline-block; padding: 8px 18px; background: #667eea; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; transition: all .2s; text-decoration: none; }
.btn:hover { background: #5568d3; transform: translateY(-1px); }
.btn-sec { background: #6c757d; }
.btn-sec:hover { background: #5a6268; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn:disabled { background: #aaa; cursor: not-allowed; transform: none; }
.section-tabs { display: flex; gap: 4px; border-bottom: 1px solid #e0e0e0; margin-bottom: 16px; }
.tab { padding: 10px 18px; cursor: pointer; border-bottom: 2px solid transparent; color: #888; font-size: 14px; }
.tab.active { color: #667eea; border-bottom-color: #667eea; font-weight: 600; }
.tab-content { display: none; }
.tab-content.active { display: block; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #f0f0f0; }
th { background: #f8f9fa; font-weight: 600; color: #555; position: sticky; top: 0; }
tr:hover { background: #fafbfc; }
tr { cursor: pointer; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.tag-success { background: #d4edda; color: #155724; }
.tag-warn { background: #fff3cd; color: #856404; }
.tag-fail { background: #f8d7da; color: #721c24; }
.tag-info { background: #d1ecf1; color: #0c5460; }
.content-cell { max-width: 300px; max-height: 80px; overflow: auto; white-space: pre-wrap; word-break: break-all; color: #555; }
.muted { color: #999; }
.sidebar { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.06); max-height: 75vh; overflow-y: auto; }
.sidebar h3 { font-size: 14px; margin-bottom: 10px; color: #555; }
.sidebar-item { padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }
.sidebar-item:hover { background: #f0f3ff; }
.sidebar-item.active { background: #667eea; color: #fff; }
.sidebar-item .cnt { background: rgba(0,0,0,.08); padding: 1px 7px; border-radius: 10px; font-size: 11px; }
.sidebar-item.active .cnt { background: rgba(255,255,255,.25); }
.main-area { flex: 3; min-width: 0; }
.side-area { flex: 1; min-width: 240px; }
.detail-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,.5); z-index: 100; }
.detail-modal { display: none; position: fixed; top: 4%; left: 50%; transform: translateX(-50%); width: 90%; max-width: 900px; max-height: 88vh; background: #fff; border-radius: 10px; z-index: 101; overflow: hidden; }
.detail-header { padding: 16px 24px; background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; display: flex; justify-content: space-between; align-items: center; }
.detail-body { padding: 20px 24px; overflow-y: auto; max-height: calc(88vh - 60px); }
.detail-close { cursor: pointer; font-size: 24px; line-height: 1; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 20px; }
.detail-item { padding: 10px; background: #f8f9fa; border-radius: 6px; border-left: 3px solid #667eea; }
.detail-item .lbl { font-size: 12px; color: #888; margin-bottom: 4px; }
.detail-item .val { font-size: 14px; color: #333; word-break: break-all; }
.detail-item.full { grid-column: 1 / -1; }
.detail-item .val.semantic { color: #667eea; font-weight: 500; }
.detail-item .val.raw { color: #888; font-size: 13px; white-space: pre-wrap; }
.toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; background: #333; color: #fff; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 9999; }
.toast.show { opacity: 1; }
.toast.err { background: #dc3545; }
.toast.ok { background: #28a745; }
.pager { margin-top: 12px; text-align: center; }
.pager button { margin: 0 4px; }
.empty { text-align: center; padding: 30px; color: #999; font-size: 14px; }
.bar-mini { display: inline-block; width: 80px; height: 8px; background: #e9ecef; border-radius: 4px; overflow: hidden; vertical-align: middle; margin-left: 6px; }
.bar-mini-fill { height: 100%; background: #667eea; }
.tree-node { margin-bottom: 2px; }
.tree-parent { padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px; margin-bottom: 2px; }
.tree-parent:hover { background: #f0f3ff; }
.tree-arrow { font-size: 10px; transition: transform 0.2s; width: 12px; }
.tree-children { margin-left: 20px; padding-left: 10px; border-left: 2px solid #e0e0e0; }
.tree-child { padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }
.tree-child:hover { background: #f0f3ff; }
.tree-child .cnt { background: rgba(0,0,0,.08); padding: 1px 7px; border-radius: 10px; font-size: 11px; }
.tree-parent .cnt { background: rgba(0,0,0,.08); padding: 1px 7px; border-radius: 10px; font-size: 11px; margin-left: auto; }
</style>
</head>
<body>
<div class="header">
    <h1>工单数据浏览</h1>
    <div class="sub">详细清洗结果 · 按主体分类 · 按地点分类 · 单工单详情</div>
    <div style="margin-top:10px;">
        <a href="/" class="btn btn-sm" style="background:rgba(255,255,255,.2);">清洗管理</a>
        <a href="/data" class="btn btn-sm" style="background:rgba(255,255,255,.2);">数据浏览</a>
    </div>
</div>

<div class="container">

<!-- 查询条件 -->
<div class="card">
    <div class="row" style="align-items:flex-end;">
        <div class="col">
            <label>Job ID *</label>
            <input type="text" id="jobId" placeholder="输入Job ID，如 smoke-test">
        </div>
        <div class="col">
            <label>关键词搜索</label>
            <input type="text" id="keyword" placeholder="搜索原始/清洗/语义内容">
        </div>
        <div class="col">
            <label>事件类型</label>
            <select id="eventType"><option value="">全部</option></select>
        </div>
        <div class="col">
            <label>每页条数</label>
            <select id="pageSize">
                <option value="20">20</option>
                <option value="50" selected>50</option>
                <option value="100">100</option>
            </select>
        </div>
        <div class="col" style="display:flex;gap:8px;">
            <button class="btn" onclick="loadData(0)">查询</button>
            <button class="btn btn-sec" onclick="resetFilters()">重置</button>
        </div>
    </div>
</div>

<!-- Tabs -->
<div class="card">
    <div class="section-tabs">
        <div class="tab active" data-tab="list">工单列表</div>
        <div class="tab" data-tab="org">按主体分类</div>
        <div class="tab" data-tab="addr">按地点分类</div>
    </div>

    <!-- 工单列表 -->
    <div class="tab-content active" id="tab-list">
        <div class="row">
            <div class="col main-area">
                <h2 style="font-size:14px;margin-bottom:10px;color:#555;">清洗结果 <span id="totalInfo" class="muted"></span></h2>
                <div style="overflow:auto;max-height:70vh;border:1px solid #f0f0f0;border-radius:6px;">
                <table id="listTable">
                    <thead><tr>
                        <th>#</th><th>工单号</th><th>场景</th><th>性质</th><th>清洗内容</th><th>语义内容</th>
                        <th>主体</th><th>地点</th><th>事件</th><th>诉求</th>
                        <th>提交人</th><th>电话</th><th>时间</th><th>质量分</th><th>可用</th>
                    </tr></thead>
                    <tbody id="listBody"><tr><td colspan="15" class="empty">请先填写 Job ID 并点击查询</td></tr></tbody>
                </table>
                </div>
                <div class="pager" id="pager"></div>
            </div>
            <div class="col side-area">
                <div class="sidebar">
                    <h3>事件类型筛选</h3>
                    <div id="eventSidebar"><div class="muted">查询后显示</div></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 按主体分类 -->
    <div class="tab-content" id="tab-org">
        <div class="row">
            <div class="col side-area">
                <div class="sidebar">
                    <h3>主体列表（点击查看工单）</h3>
                    <div id="orgSidebar"><div class="muted">查询后显示</div></div>
                </div>
            </div>
            <div class="col main-area">
                <h2 style="font-size:14px;margin-bottom:10px;color:#555;" id="orgTitle">主体工单明细</h2>
                <div style="overflow:auto;max-height:70vh;border:1px solid #f0f0f0;border-radius:6px;">
                <table>
                    <thead><tr>
                        <th>工单号</th><th>清洗内容</th><th>语义内容</th>
                        <th>地点</th><th>事件</th><th>诉求</th><th>时间</th><th>质量分</th>
                    </tr></thead>
                    <tbody id="orgBody"><tr><td colspan="8" class="empty">点击左侧主体查看</td></tr></tbody>
                </table>
                </div>
            </div>
        </div>
    </div>

    <!-- 按地点分类 -->
    <div class="tab-content" id="tab-addr">
        <div class="row">
            <div class="col side-area">
                <div class="sidebar">
                    <h3>地点树形结构（镇街 → 小区）</h3>
                    <div id="addrTree"><div class="muted">查询后显示</div></div>
                </div>
            </div>
            <div class="col main-area">
                <h2 style="font-size:14px;margin-bottom:10px;color:#555;" id="addrTitle">地点工单明细</h2>
                <div style="overflow:auto;max-height:70vh;border:1px solid #f0f0f0;border-radius:6px;">
                <table>
                    <thead><tr>
                        <th>工单号</th><th>清洗内容</th><th>语义内容</th>
                        <th>主体</th><th>事件</th><th>诉求</th><th>时间</th><th>质量分</th>
                    </tr></thead>
                    <tbody id="addrBody"><tr><td colspan="8" class="empty">点击左侧地点查看</td></tr></tbody>
                </table>
                </div>
            </div>
        </div>
    </div>
</div>

</div>

<!-- 详情弹窗 -->
<div class="detail-overlay" id="detailOverlay" onclick="closeDetail()"></div>
<div class="detail-modal" id="detailModal">
    <div class="detail-header">
        <div>
            <span id="detailTicketNo" style="font-weight:600;font-size:18px;"></span>
            <span id="detailStatus" style="margin-left:10px;font-size:12px;"></span>
        </div>
        <span class="detail-close" onclick="closeDetail()">&times;</span>
    </div>
    <div class="detail-body" id="detailBody"></div>
</div>

<div class="toast" id="toast"></div>

<script>
function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || '');
    setTimeout(() => t.className = 'toast ' + (type || ''), 2500);
}
document.querySelectorAll('.tab').forEach(tab => {
    tab.onclick = () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        const job = document.getElementById('jobId').value.trim();
        if (job) {
            if (tab.dataset.tab === 'org') loadOrgGroups();
            if (tab.dataset.tab === 'addr') loadAddrGroups();
        }
    };
});
function statusTag(s) {
    const m = {success:'tag-success', partial:'tag-warn', failed:'tag-fail'};
    return '<span class="tag ' + (m[s]||'tag-info') + '">' + s + '</span>';
}
function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function short(s, n) {
    if (!s) return '<span class="muted">-</span>';
    s = String(s);
    if (s.length <= n) return esc(s);
    return esc(s.substring(0, n)) + '...';
}
function getRequestNatureTag(nature) {
    const m = {
        complaint: '<span class="tag tag-fail">投诉</span>',
        suggestion: '<span class="tag tag-info">建议</span>',
        report: '<span class="tag tag-warn">举报</span>',
        consultation: '<span class="tag tag-success">咨询</span>',
        help: '<span class="tag" style="background:#e2e3f5;color:#383d6e;">求助</span>',
    };
    return m[nature] || '<span class="muted">-</span>';
}

// ===== 列表查询 =====
let curPage = 0;
let curOrg = '', curTown = '', curCommunity = '', curEvent = '', curKeyword = '';
let curAddrName = '', curAddrLevel = 'town';

function loadData(page) {
    const job = document.getElementById('jobId').value.trim();
    if (!job) { toast('请填写 Job ID', 'err'); return; }
    curPage = page || 0;
    const size = parseInt(document.getElementById('pageSize').value);
    curKeyword = document.getElementById('keyword').value.trim();
    curEvent = document.getElementById('eventType').value;

    const params = new URLSearchParams({
        job_id: job, page: curPage, size: size,
    });
    if (curOrg) params.set('organization', curOrg);
    if (curTown) params.set('town', curTown);
    if (curCommunity) params.set('community', curCommunity);
    if (curEvent) params.set('event_type', curEvent);
    if (curKeyword) params.set('keyword', curKeyword);

    fetch('/api/search_cleaned?' + params.toString())
    .then(r => r.json()).then(r => {
        renderList(r.data || [], r.total || 0, size);
        loadEventSidebar(job);
        // 同时预加载主体和地点侧栏
        loadOrgGroups();
        loadAddrGroups();
    }).catch(e => toast('查询失败: ' + e, 'err'));
}

function renderList(data, total, size) {
    document.getElementById('totalInfo').textContent = '· 共 ' + total + ' 条';
    const tb = document.getElementById('listBody');
    if (!data.length) { tb.innerHTML = '<tr><td colspan="14" class="empty">无数据</td></tr>'; document.getElementById('pager').innerHTML=''; return; }
    let html = '';
    data.forEach((d, i) => {
        const idx = curPage * size + i + 1;
        const usable = d.is_usable_for_duplicate ? '<span class="tag tag-success">是</span>' : '<span class="tag tag-fail">否</span>';
        const ticketType = d.ticket_type === 'online' ? '<span class="tag tag-info">线上</span>' :
                          d.ticket_type === 'offline' ? '<span class="tag tag-warn">线下</span>' :
                          '<span class="muted">-</span>';
        const requestNature = getRequestNatureTag(d.request_nature);
        html += '<tr onclick="showDetail(\\''+esc(d.ticket_no)+'\\',\\''+esc(d.job_id)+'\\')">'
            + '<td>' + idx + '</td>'
            + '<td>' + esc(d.ticket_no) + '</td>'
            + '<td>' + ticketType + '</td>'
            + '<td>' + requestNature + '</td>'
            + '<td class="content-cell">' + short(d.clean_content, 80) + '</td>'
            + '<td class="content-cell" style="color:#667eea;">' + esc(d.semantic_content) + '</td>'
            + '<td>' + (d.organization_normalized ? esc(d.organization_normalized) : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.address_normalized ? esc(d.address_normalized) : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.event_type ? '<span class="tag tag-info">'+esc(d.event_type)+'</span>' : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.request ? esc(d.request) : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.person_raw ? esc(d.person_raw) : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.phone_masked ? esc(d.phone_masked) : '<span class="muted">-</span>') + '</td>'
            + '<td>' + (d.time_start ? esc(d.time_start.substring(0,16)) : (d.time_pattern||'<span class="muted">-</span>')) + '</td>'
            + '<td>' + (d.data_quality_score||0).toFixed(2) + '</td>'
            + '<td>' + usable + '</td></tr>';
    });
    tb.innerHTML = html;

    const totalPages = Math.ceil(total / size);
    let p = '<button class="btn btn-sm btn-sec" onclick="loadData('+Math.max(0,curPage-1)+')">上一页</button> ';
    p += '<span>第 ' + (curPage+1) + ' / ' + totalPages + ' 页 · 共 ' + total + ' 条</span> ';
    p += '<button class="btn btn-sm btn-sec" onclick="loadData('+Math.min(totalPages-1,curPage+1)+')">下一页</button>';
    document.getElementById('pager').innerHTML = p;
}

function resetFilters() {
    document.getElementById('keyword').value = '';
    document.getElementById('eventType').value = '';
    curOrg = ''; curTown = ''; curCommunity = ''; curEvent = ''; curKeyword = '';
    document.querySelectorAll('.sidebar-item').forEach(e => e.classList.remove('active'));
    loadData(0);
}

// ===== 事件侧栏 =====
function loadEventSidebar(job) {
    fetch('/api/event_types?job_id=' + encodeURIComponent(job))
    .then(r => r.json()).then(r => {
        const el = document.getElementById('eventSidebar');
        const sel = document.getElementById('eventType');
        const types = r.types || [];
        // 填充select
        const curVal = sel.value;
        sel.innerHTML = '<option value="">全部事件</option>';
        types.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.name; opt.textContent = t.name + ' (' + t.cnt + ')';
            sel.appendChild(opt);
        });
        sel.value = curVal;
        // 填充侧栏
        if (!types.length) { el.innerHTML = '<div class="muted">无</div>'; return; }
        let html = '<div class="sidebar-item' + (curEvent===''?' active':'') + '" onclick="filterByEvent(\\'\\')"><span>全部</span></div>';
        types.forEach(t => {
            const max = types[0].cnt;
            const pct = Math.round(t.cnt / max * 100);
            html += '<div class="sidebar-item' + (curEvent===t.name?' active':'') + '" onclick="filterByEvent(\\''+esc(t.name)+'\\')">'
                + '<span>' + esc(t.name) + '</span><span class="cnt">' + t.cnt + '</span></div>';
        });
        el.innerHTML = html;
    });
}

function filterByEvent(name) {
    curEvent = name;
    document.getElementById('eventType').value = name;
    loadData(0);
}

// ===== 按主体分类 =====
function loadOrgGroups() {
    const job = document.getElementById('jobId').value.trim();
    if (!job) return;
    fetch('/api/group_by_organization?job_id=' + encodeURIComponent(job))
    .then(r => r.json()).then(r => {
        const el = document.getElementById('orgSidebar');
        const groups = r.groups || [];
        if (!groups.length) { el.innerHTML = '<div class="muted">无主体识别结果</div>'; return; }
        let html = '';
        groups.forEach(g => {
            html += '<div class="sidebar-item' + (curOrg===g.name?' active':'') + '" onclick="filterByOrg(\\''+esc(g.name)+'\\')">'
                + '<span>' + esc(g.name) + '</span><span class="cnt">' + g.cnt + '</span></div>';
        });
        el.innerHTML = html;
    });
}

function filterByOrg(name) {
    curOrg = name; curTown=''; curCommunity='';
    document.querySelectorAll('#orgSidebar .sidebar-item').forEach(e => e.classList.remove('active'));
    if (name) event.target && event.target.closest && event.target.closest('.sidebar-item') && event.target.closest('.sidebar-item').classList.add('active');
    // 直接查询该主体下的工单
    const job = document.getElementById('jobId').value.trim();
    const params = new URLSearchParams({job_id: job, page: 0, size: 100, organization: name});
    fetch('/api/search_cleaned?' + params.toString())
    .then(r => r.json()).then(r => {
        document.getElementById('orgTitle').textContent = '主体「' + name + '」工单明细 · ' + r.total + ' 条';
        const tb = document.getElementById('orgBody');
        if (!r.data.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">无数据</td></tr>'; return; }
        let html = '';
        r.data.forEach(d => {
            html += '<tr onclick="showDetail(\\''+esc(d.ticket_no)+'\\',\\''+esc(d.job_id)+'\\')">'
                + '<td>' + esc(d.ticket_no) + '</td>'
                + '<td class="content-cell">' + short(d.clean_content, 80) + '</td>'
                + '<td class="content-cell" style="color:#667eea;">' + esc(d.semantic_content) + '</td>'
                + '<td>' + (d.address_normalized||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.event_type?'<span class="tag tag-info">'+esc(d.event_type)+'</span>':'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.request||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.time_start?esc(d.time_start.substring(0,16)):(d.time_pattern||'')) + '</td>'
                + '<td>' + (d.data_quality_score||0).toFixed(2) + '</td></tr>';
        });
        tb.innerHTML = html;
    });
}

// ===== 按地点分类（树形结构）=====
function loadAddrGroups() {
    const job = document.getElementById('jobId').value.trim();
    if (!job) return;
    fetch('/api/group_by_town_tree?job_id=' + encodeURIComponent(job))
    .then(r => r.json()).then(r => {
        const el = document.getElementById('addrTree');
        const tree = r.tree || [];
        if (!tree.length) { el.innerHTML = '<div class="muted">无地点识别结果</div>'; return; }
        let html = '';
        tree.forEach(town => {
            const hasChildren = town.children && town.children.length > 0;
            html += '<div class="tree-node">';
            html += '<div class="tree-parent" onclick="toggleTree(this)">';
            html += '<span class="tree-arrow">▶</span>';
            html += '<span>' + esc(town.name) + '</span>';
            html += '<span class="cnt">' + town.cnt + '</span>';
            html += '</div>';
            if (hasChildren) {
                html += '<div class="tree-children" style="display:none;">';
                town.children.forEach(comm => {
                    html += '<div class="tree-child" onclick="filterByAddr(\\''+esc(town.name)+'\\',\\''+esc(comm.name)+'\\')">';
                    html += '<span>' + esc(comm.name) + '</span>';
                    html += '<span class="cnt">' + comm.cnt + '</span>';
                    html += '</div>';
                });
                html += '</div>';
            }
            html += '</div>';
        });
        el.innerHTML = html;
    });
}

function toggleTree(elem) {
    const children = elem.nextElementSibling;
    const arrow = elem.querySelector('.tree-arrow');
    if (children && children.classList.contains('tree-children')) {
        if (children.style.display === 'none') {
            children.style.display = 'block';
            arrow.textContent = '▼';
        } else {
            children.style.display = 'none';
            arrow.textContent = '▶';
        }
    }
}

function filterByAddr(town, community) {
    curAddrName = community || town;
    const job = document.getElementById('jobId').value.trim();
    const params = new URLSearchParams({job_id: job, page: 0, size: 100});
    if (town) params.set('town', town);
    if (community) params.set('community', community);
    fetch('/api/search_cleaned?' + params.toString())
    .then(r => r.json()).then(r => {
        const title = community ? `地点「${town} - ${community}」` : `地点「${town}」`;
        document.getElementById('addrTitle').textContent = `${title}工单明细 · ${r.total} 条`;
        const tb = document.getElementById('addrBody');
        if (!r.data.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">无数据</td></tr>'; return; }
        let html = '';
        r.data.forEach(d => {
            html += '<tr onclick="showDetail(\\''+esc(d.ticket_no)+'\\',\\''+esc(d.job_id)+'\\')">'
                + '<td>' + esc(d.ticket_no) + '</td>'
                + '<td class="content-cell">' + short(d.clean_content, 80) + '</td>'
                + '<td class="content-cell" style="color:#667eea;">' + esc(d.semantic_content) + '</td>'
                + '<td>' + (d.organization_normalized||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.event_type?'<span class="tag tag-info">'+esc(d.event_type)+'</span>':'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.request||'<span class="muted">-</span>') + '</td>'
                + '<td>' + (d.time_start?esc(d.time_start.substring(0,16)):(d.time_pattern||'')) + '</td>'
                + '<td>' + (d.data_quality_score||0).toFixed(2) + '</td></tr>';
        });
        tb.innerHTML = html;
    });
}

// ===== 详情弹窗 =====
function showDetail(ticketNo, jobId) {
    fetch('/api/ticket_detail?job_id=' + encodeURIComponent(jobId) + '&ticket_no=' + encodeURIComponent(ticketNo))
    .then(r => r.json()).then(d => {
        if (!d.ticket_no) { toast('未找到工单', 'err'); return; }
        document.getElementById('detailTicketNo').textContent = d.ticket_no;
        document.getElementById('detailStatus').innerHTML = statusTag(d.parse_status) + ' 质量分 ' + (d.data_quality_score||0).toFixed(2);
        const phone = d.phone_masked || d.phone_normalized || d.phone_raw || '<span class="muted">未识别</span>';
        const person = d.person_raw || '<span class="muted">未识别</span>';
        const time = d.time_start || (d.time_pattern ? '周期：' + d.time_pattern : '<span class="muted">未识别</span>');
        const org = d.organization_normalized || '<span class="muted">未识别</span>';
        const addr = d.address_normalized || '<span class="muted">未识别</span>';
        let html = '<div class="detail-grid">';
        html += detailItem('提交人', person);
        html += detailItem('电话号码', phone);
        html += detailItem('主体（归一化）', org + (d.organization_raw && d.organization_raw !== d.organization_normalized ? '<br><span class="muted">原文：'+esc(d.organization_raw)+'</span>' : '') + (d.organization_confidence?' <span class="muted">置信度 '+d.organization_confidence.toFixed(2)+'</span>':''));
        html += detailItem('地点（归一化）', addr);
        html += detailItem('行政区', esc(d.district) || '<span class="muted">-</span>');
        html += detailItem('镇街', esc(d.town) || '<span class="muted">-</span>');
        html += detailItem('小区', esc(d.community) || '<span class="muted">-</span>');
        html += detailItem('道路', esc(d.road) || '<span class="muted">-</span>');
        html += detailItem('门牌号', esc(d.building) || '<span class="muted">-</span>');
        html += detailItem('事件类型', d.event_type ? '<span class="tag tag-info">'+esc(d.event_type)+'</span>' : '<span class="muted">未分类</span>');
        html += detailItem('工单类型', d.ticket_type === 'online' ? '<span class="tag tag-info">线上</span>' : d.ticket_type === 'offline' ? '<span class="tag tag-warn">线下</span>' : '<span class="muted">未知</span>');
        html += detailItem('诉求性质', getRequestNatureTag(d.request_nature));
        html += detailItem('事件行为', esc(d.event_action) || '<span class="muted">-</span>');
        html += detailItem('事件主体', esc(d.event_subject) || '<span class="muted">-</span>');
        html += detailItem('诉求', esc(d.request) || '<span class="muted">未识别</span>');
        html += detailItem('问题简述', esc(d.issue) || '<span class="muted">-</span>');
        html += detailItem('时间', time);
        html += detailItem('电话置信度', d.phone_match_confidence ? d.phone_match_confidence.toFixed(2) : '<span class="muted">-</span>');
        html += detailItem('人物置信度', d.person_confidence ? d.person_confidence.toFixed(2) : '<span class="muted">-</span>');
        html += detailItem('是否可用于重复判断', d.is_usable_for_duplicate ? '<span class="tag tag-success">是</span>' : '<span class="tag tag-fail">否</span>');
        html += detailItem('处理时间', esc(d.processed_at));
        html += detailItem('Pipeline版本', esc(d.pipeline_version));
        html += detailItem('内容Hash', '<span style="font-family:monospace;font-size:11px;">'+esc(d.content_hash)+'</span>');
        html += detailItem('语义内容', esc(d.semantic_content) || '<span class="muted">-</span>', true, 'semantic');
        html += detailItem('清洗内容', esc(d.clean_content) || '<span class="muted">-</span>', true, 'raw');
        html += detailItem('原始内容', esc(d.raw_content) || '<span class="muted">-</span>', true, 'raw');
        html += '</div>';
        document.getElementById('detailBody').innerHTML = html;
        document.getElementById('detailOverlay').style.display = 'block';
        document.getElementById('detailModal').style.display = 'block';
    });
}

function detailItem(lbl, val, full, cls) {
    return '<div class="detail-item' + (full?' full':'') + '"><div class="lbl">' + lbl + '</div><div class="val ' + (cls||'') + '">' + val + '</div></div>';
}

function closeDetail() {
    document.getElementById('detailOverlay').style.display = 'none';
    document.getElementById('detailModal').style.display = 'none';
}

// ESC关闭弹窗
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

// URL参数自动填充Job ID
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('job_id')) {
    document.getElementById('jobId').value = urlParams.get('job_id');
    loadData(0);
}
</script>
</body>
</html>
"""


@app.route("/data")
def data_page():
    return render_template_string(DATA_PAGE)


@app.route("/api/search_cleaned")
def search_cleaned_api():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(data=[], total=0)
    page = int(request.args.get("page", 0))
    size = int(request.args.get("size", 50))
    storage = Storage(DB_PATH)
    rows, total = storage.search_cleaned(
        job_id,
        organization=request.args.get("organization") or None,
        town=request.args.get("town") or None,
        community=request.args.get("community") or None,
        event_type=request.args.get("event_type") or None,
        keyword=request.args.get("keyword") or None,
        usable_only=request.args.get("usable_only") == "1",
        limit=size,
        offset=page * size,
    )
    return jsonify(data=rows, total=total)


@app.route("/api/group_by_organization")
def group_by_org_api():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(groups=[])
    storage = Storage(DB_PATH)
    groups = storage.group_by_organization(job_id, min_count=1, limit=500)
    # ticket_nos 是逗号分隔，转为数组长度
    for g in groups:
        if g.get("ticket_nos"):
            g["ticket_nos"] = g["ticket_nos"].split(",")
        else:
            g["ticket_nos"] = []
    return jsonify(groups=groups)


@app.route("/api/group_by_address")
def group_by_addr_api():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(groups=[])
    level = request.args.get("level", "town")
    storage = Storage(DB_PATH)
    groups = storage.group_by_address(job_id, level=level, min_count=1, limit=500)
    for g in groups:
        if g.get("ticket_nos"):
            g["ticket_nos"] = g["ticket_nos"].split(",")
        else:
            g["ticket_nos"] = []
    return jsonify(groups=groups)


@app.route("/api/group_by_town_tree")
def group_by_town_tree_api():
    """返回按镇街分组的树形结构，每个镇街下包含小区列表。"""
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(tree=[])
    storage = Storage(DB_PATH)
    tree = storage.group_by_town_tree(job_id, min_count=1)
    # 转换ticket_nos为数组
    for town in tree:
        if town.get("ticket_nos"):
            town["ticket_nos"] = town["ticket_nos"].split(",")
        else:
            town["ticket_nos"] = []
        for community in town.get("children", []):
            if community.get("ticket_nos"):
                community["ticket_nos"] = community["ticket_nos"].split(",")
            else:
                community["ticket_nos"] = []
    return jsonify(tree=tree)


@app.route("/api/event_types")
def event_types_api():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(types=[])
    storage = Storage(DB_PATH)
    types = storage.list_event_types(job_id)
    return jsonify(types=types)


@app.route("/api/ticket_detail")
def ticket_detail_api():
    job_id = request.args.get("job_id")
    ticket_no = request.args.get("ticket_no")
    if not job_id or not ticket_no:
        return jsonify({})
    storage = Storage(DB_PATH)
    d = storage.get_cleaned_by_ticket(ticket_no, job_id)
    if d:
        d.pop("embedding", None)
    return jsonify(d or {})


@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify(ok=False, error="未收到文件")
    file = request.files["file"]
    if not file.filename:
        return jsonify(ok=False, error="文件名为空")
    filename = file.filename
    # 安全文件名
    safe_name = os.path.basename(filename)
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    try:
        file.save(save_path)
        # 读取记录数
        try:
            reader = ExcelReader(save_path)
            records = reader.count()
        except Exception as e:
            records = 0
        return jsonify(
            ok=True,
            filename=safe_name,
            path=save_path,
            records=records,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/set_source", methods=["POST"])
def set_source():
    data = request.get_json() or {}
    with _state_lock:
        _state["current_source"] = {
            "path": data.get("path"),
            "filename": data.get("filename"),
            "records": data.get("records", 0),
        }
    return jsonify(ok=True)


@app.route("/api/current_source")
def current_source():
    with _state_lock:
        s = _state.get("current_source", {})
    return jsonify(s)


@app.route("/api/create_job", methods=["POST"])
def create_job():
    data = request.get_json() or {}
    job_id = data.get("job_id") or f"job-{int(time.time())}"
    batch_size = int(data.get("batch_size", 500))
    with _state_lock:
        src = _state.get("current_source", {})
    path = src.get("path") if src else None
    if not path or not os.path.exists(path):
        return jsonify(ok=False, error="请先上传数据文件")
    try:
        cfg = _cfg_with_source(path, batch_size)
        engine = BatchEngine(cfg)
        info = engine.create_job(job_id)
        return jsonify(ok=True, **info)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/jobs")
def list_jobs():
    storage = Storage(DB_PATH)
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cleaning_job ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return jsonify(jobs=[dict(r) for r in rows])


@app.route("/api/run_job", methods=["POST"])
def run_job_api():
    data = request.get_json() or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify(ok=False, error="缺少 job_id")
    # 在后台线程运行
    with _state_lock:
        if job_id in _state["job_threads"] and _state["job_threads"][job_id].is_alive():
            return jsonify(ok=False, error="任务正在运行中")
        # 读取job配置
        storage = Storage(DB_PATH)
        job = storage.get_job(job_id)
        if not job:
            return jsonify(ok=False, error="任务不存在")
        # 用job的batch_size和默认源
        src = _state.get("current_source", {})
        path = src.get("path") if src else None
        if not path or not os.path.exists(path):
            return jsonify(ok=False, error="请先通过 /api/upload 上传数据文件")
        cfg = _cfg_with_source(path, job["batch_size"])

        def _run():
            engine = BatchEngine(cfg)
            with _state_lock:
                _state["job_engines"][job_id] = engine
            def on_progress(p: ProgressInfo):
                with _state_lock:
                    _state["active_jobs"][job_id] = p.as_dict()
            try:
                engine.run_job(job_id, on_progress=on_progress)
            except Exception as e:
                with _state_lock:
                    _state["active_jobs"][job_id] = {
                        "status": "FAILED", "message": str(e)
                    }
            finally:
                with _state_lock:
                    _state["job_engines"].pop(job_id, None)

        t = threading.Thread(target=_run, daemon=True)
        _state["job_threads"][job_id] = t
        t.start()
    return jsonify(ok=True, message="任务已启动")


@app.route("/api/job_progress/<job_id>")
def job_progress(job_id):
    with _state_lock:
        p = _state["active_jobs"].get(job_id)
    return jsonify(p or {})


@app.route("/api/batches")
def list_batches():
    job_id = request.args.get("job_id")
    status = request.args.get("status")
    if not job_id:
        return jsonify(batches=[])
    storage = Storage(DB_PATH)
    batches = storage.list_batches(job_id, status=status)
    return jsonify(batches=batches)


@app.route("/api/retry_batch", methods=["POST"])
def retry_batch_api():
    data = request.get_json() or {}
    job_id = data.get("job_id")
    batch_no = int(data.get("batch_no", 0))
    if not job_id or not batch_no:
        return jsonify(ok=False, error="参数错误")
    storage = Storage(DB_PATH)
    job = storage.get_job(job_id)
    if not job:
        return jsonify(ok=False, error="任务不存在")
    with _state_lock:
        src = _state.get("current_source", {})
    path = src.get("path") if src else None
    if not path or not os.path.exists(path):
        return jsonify(ok=False, error="请先通过 /api/upload 上传数据文件")
    cfg = _cfg_with_source(path, job["batch_size"])

    def _run():
        engine = BatchEngine(cfg)
        def on_progress(p: ProgressInfo):
            with _state_lock:
                _state["active_jobs"][job_id] = p.as_dict()
        engine.retry_batch(job_id, batch_no, on_progress=on_progress)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/stop_job", methods=["POST"])
def stop_job_api():
    """停止正在运行的任务。"""
    data = request.get_json() or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify(ok=False, error="缺少 job_id")
    
    with _state_lock:
        # 检查任务是否在运行
        if job_id not in _state["job_threads"] or not _state["job_threads"][job_id].is_alive():
            return jsonify(ok=False, error="任务未在运行中")
        
        # 获取运行中的引擎实例
        engine = _state["job_engines"].get(job_id)
        if not engine:
            return jsonify(ok=False, error="引擎实例不存在")
        
        # 请求停止
        if engine.request_stop(job_id):
            return jsonify(ok=True, message="已发送停止请求")
        else:
            return jsonify(ok=False, error="无法停止任务")


@app.route("/api/results")
def results():
    job_id = request.args.get("job_id")
    page = int(request.args.get("page", 0))
    size = int(request.args.get("size", 20))
    if not job_id:
        return jsonify(data=[], total=0)
    storage = Storage(DB_PATH)
    offset = page * size
    rows = storage.get_cleaned(job_id, limit=size, offset=offset)
    # 总数
    with storage._conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=?", (job_id,)
        ).fetchone()["c"]
    # 把 embedding 字段去掉
    for r in rows:
        r.pop("embedding", None)
    return jsonify(data=rows, total=total)


@app.route("/api/duplicates", methods=["POST"])
def duplicates_api():
    data = request.get_json() or {}
    job_id = data.get("job_id")
    top_k = int(data.get("top_k", 20))
    if not job_id:
        return jsonify(ok=False, error="缺少 job_id")
    try:
        storage = Storage(DB_PATH)
        detector = DuplicateDetector(storage)
        cands = detector.find_candidates(job_id, top_k=top_k, max_pairs=100)
        return jsonify(
            ok=True,
            candidates=[
                {
                    "ticket_no_a": c.ticket_no_a,
                    "ticket_no_b": c.ticket_no_b,
                    "similarity": c.similarity,
                    "duplicate": c.duplicate,
                    "reason": c.reason,
                    "details": c.details,
                }
                for c in cands
            ],
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/stats")
def stats():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify(job=None)
    storage = Storage(DB_PATH)
    s = storage.job_stats(job_id)
    return jsonify(s)


if __name__ == "__main__":
    print("=" * 60)
    print("12345 工单清洗平台")
    print("访问: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
