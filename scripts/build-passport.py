#!/usr/bin/env python3
"""Build a three-page Russian project passport from verified evidence.

Usage: python3 scripts/build-passport.py --evidence docs/evidence/summary.json
       python3 scripts/build-passport.py --print-schema

No sample results are embedded. Evidence must cover exactly two VMs; each report
must already exist in the repository. Version values come from versions.yaml.
The JSON example returned by --print-schema describes types, not test results.
After building, render all pages with pdftoppm and visually review them.
"""
from __future__ import annotations

import argparse
from datetime import date
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {
    "tested_at": "YYYY-MM-DD",
    "source_revision": "Commit SHA or precise description of tested source snapshot",
    "repository_url": "HTTPS URL of published main branch, or null if unpublished",
    "vms": [{
        "name": "VM A / VM B", "os": "Actual OS and architecture",
        "vcpu": "positive integer", "ram_gib": "positive number", "disk_gb": "positive number",
        "clean_deploy": "boolean", "repeat_deploy": "boolean",
        "checks_passed": "nonnegative integer", "checks_total": "positive integer",
        "canary": {"samples": "positive integer", "v1": "nonnegative integer", "v2": "nonnegative integer"},
        "prometheus_targets_up": "positive integer", "loki_stdout": "boolean", "loki_stderr": "boolean",
        "report": "Existing repository-relative evidence report path"
    }, "Second VM object with the same fields"],
    "fault_tests": [{"name": "Actual fault/recovery scenario", "result": "PASS, FAIL or NOT_RUN", "report": "Existing repository-relative report path"}],
    "unit_tests": {"passed": "nonnegative integer", "total": "positive integer"}
}


def evidence_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Evidence report path must be a nonempty string")
    result = (ROOT / value).resolve()
    if not result.is_relative_to(ROOT) or not result.is_file():
        raise ValueError(f"Evidence report does not exist inside repository: {value}")
    return result


def integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def bounded_text(value, label, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must contain 1..{limit} characters")
    return value


def load_evidence(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    date.fromisoformat(data["tested_at"])
    bounded_text(data["source_revision"], "source_revision", 120)
    url = data.get("repository_url")
    if url is not None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("repository_url must be a public HTTPS URL or null")
    vms = data["vms"]
    if not isinstance(vms, list) or len(vms) != 2:
        raise ValueError("Exactly two actual VM evidence objects are required")
    for vm in vms:
        bounded_text(vm["name"], "VM name", 32)
        bounded_text(vm["os"], "OS", 64)
        integer(vm["vcpu"], "vcpu", 1)
        for key in ("ram_gib", "disk_gb"):
            if type(vm[key]) not in (int, float) or not 0 < vm[key] < 1_000_000:
                raise ValueError(f"Invalid {key}")
        passed = integer(vm["checks_passed"], "checks_passed")
        total = integer(vm["checks_total"], "checks_total", 1)
        if passed > total:
            raise ValueError("checks_passed exceeds checks_total")
        for key in ("clean_deploy", "repeat_deploy", "loki_stdout", "loki_stderr"):
            if type(vm[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        c = vm["canary"]
        if integer(c["v1"], "v1") + integer(c["v2"], "v2") != integer(c["samples"], "samples", 1):
            raise ValueError("Canary sample count must equal v1 + v2")
        integer(vm["prometheus_targets_up"], "prometheus_targets_up", 1)
        evidence_path(vm["report"])
    if vms[0]["name"] == vms[1]["name"]:
        raise ValueError("VM evidence names must be distinct")
    unit = data["unit_tests"]
    if integer(unit["passed"], "unit passed") > integer(unit["total"], "unit total", 1):
        raise ValueError("unit passed exceeds unit total")
    faults = data["fault_tests"]
    if not isinstance(faults, list) or len(faults) > 4:
        raise ValueError("Provide at most four representative fault tests")
    for item in faults:
        bounded_text(item["name"], "fault name", 85)
        if item["result"] not in ("PASS", "FAIL", "NOT_RUN"):
            raise ValueError("Fault result must be PASS, FAIL or NOT_RUN")
        evidence_path(item["report"])
    return data


def versions():
    # This repository deliberately uses simple top-level scalar version pins.
    text = (ROOT / "versions.yaml").read_text(encoding="utf-8")
    pins = dict(re.findall(r'^([a-z_]+):\s*"([^"\n]+)"\s*$', text, re.M))
    required = ("kubernetes_version", "containerd_package_version", "calico_version", "envoy_gateway_version", "gateway_api_version", "monitoring_chart_version", "loki_chart_version", "helm_version")
    if any(key not in pins for key in required):
        raise ValueError("versions.yaml is missing required scalar version pins")
    gemfile = (ROOT / "images/fluentd/Gemfile").read_text()
    pins["fluentd_version"] = re.search(r'gem "fluentd", "([^"]+)"', gemfile).group(1)
    return pins


def register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        (os.environ.get("PASSPORT_FONT_REGULAR", ""), os.environ.get("PASSPORT_FONT_BOLD", "")),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("Passport", regular))
            pdfmetrics.registerFont(TTFont("Passport-Bold", bold))
            pdfmetrics.registerFontFamily("Passport", normal="Passport", bold="Passport-Bold", italic="Passport", boldItalic="Passport-Bold")
            return
    raise ValueError("Cyrillic fonts unavailable: install fonts-dejavu-core or set PASSPORT_FONT_REGULAR/BOLD")


def build(data, pins, output):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import Paragraph, Table, TableStyle
    from pypdf import PdfReader

    register_fonts()
    width, height = A4
    margin, floor = 40, 47
    usable = width - 2 * margin
    navy, teal, ink, muted, pale = map(colors.HexColor, ("#153047", "#007D80", "#20323E", "#52616C", "#EEF5F5"))
    style = ParagraphStyle("body", fontName="Passport", fontSize=10, leading=13, textColor=ink, spaceAfter=0)
    small = ParagraphStyle("small", parent=style, fontSize=9.5, leading=12)
    centered = ParagraphStyle("centered", parent=small, alignment=TA_CENTER)
    title_style = ParagraphStyle("title", parent=style, fontName="Passport-Bold", fontSize=25, leading=29, textColor=navy)
    section_style = ParagraphStyle("section", parent=style, fontName="Passport-Bold", fontSize=13, leading=17, textColor=navy)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="passport-", suffix=".pdf", dir=output.parent)
    os.close(descriptor)
    canvas = Canvas(temporary, pagesize=A4, pageCompression=1, invariant=1)
    canvas.setTitle("Паспорт решения | MTC Engineer Hack | Резван")
    canvas.setAuthor("Резван")

    def safe(value):
        return escape(str(value).replace("—", "-").replace("–", "-").replace("‑", "-"))

    def paragraph(text, x, top, w, fmt=style):
        p = Paragraph(text, fmt)
        _, h = p.wrap(w, height)
        if top - h < floor:
            raise ValueError(f"Page overflow before rendering: {text[:70]}")
        p.drawOn(canvas, x, top - h)
        return top - h

    def section(text, y):
        return paragraph(text, margin, y, usable, section_style) - 8

    def table(rows, y, widths, header=True):
        items = [[Paragraph(cell, small) for cell in row] for row in rows]
        t = Table(items, colWidths=widths, hAlign="LEFT")
        settings = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5DFE4"))]
        if header:
            settings.append(("BACKGROUND", (0, 0), (-1, 0), pale))
        t.setStyle(TableStyle(settings))
        _, h = t.wrap(usable, height)
        if y - h < floor:
            raise ValueError(f"Table overflows page: {h:.1f}pt available {y-floor:.1f}pt")
        t.drawOn(canvas, margin, y - h)
        return y - h - 15

    def link(path, label=None):
        # The contest ZIP contains only PDF + URL text, so local file links would
        # break after extraction. Repository-relative paths remain readable.
        return safe(label or path)

    def page(number, title, intro):
        canvas.setFillColor(teal)
        canvas.rect(margin, height - 29, 34, 3, fill=1, stroke=0)
        paragraph("ПАСПОРТ РЕШЕНИЯ  /  РЕЗВАН", margin + 44, height - 25, usable - 44, small)
        y = paragraph(title, margin, height - 59, usable, title_style) - 9
        y = paragraph(intro, margin, y, usable) - 19
        canvas.setStrokeColor(colors.HexColor("#D5DFE4"))
        canvas.line(margin, 36, width - margin, 36)
        canvas.setFillColor(muted)
        canvas.setFont("Passport", 9.5)
        canvas.drawString(margin, 21, "MTC ENGINEER HACK  |  " + data["tested_at"])
        canvas.drawRightString(width - margin, 21, f"{number} / 3")
        return y

    def box(text, x, y, w=143, h=35):
        canvas.setFillColor(pale)
        canvas.setStrokeColor(colors.HexColor("#BCD5D7"))
        canvas.roundRect(x, y - h, w, h, 5, fill=1, stroke=1)
        paragraph(text, x + 5, y - 5, w - 10, centered)

    def arrow(x1, y1, x2, y2):
        import math
        canvas.setStrokeColor(teal)
        canvas.setFillColor(teal)
        canvas.setLineWidth(1.25)
        canvas.line(x1, y1, x2, y2)
        a = math.atan2(y2-y1, x2-x1)
        p = canvas.beginPath()
        p.moveTo(x2, y2)
        p.lineTo(x2-5*math.cos(a-.45), y2-5*math.sin(a-.45))
        p.lineTo(x2-5*math.cos(a+.45), y2-5*math.sin(a+.45))
        p.close()
        canvas.drawPath(p, fill=1, stroke=0)

    def yes(value):
        return "подтверждено" if value else "не подтверждено"

    try:
        y = page(1, "Kubernetes, Gateway API\nи наблюдаемость".replace("\n", "<br/>"), "Воспроизводимый стенд на Ubuntu: HTTP-запрос проходит через Envoy к Nginx и подтверждается метрикой и записью в Loki.")
        rows = [
            ["<b>Компонент</b>", "<b>Выбор в репозитории</b>"],
            ["Kubernetes / runtime", f"kubeadm {safe(pins['kubernetes_version'])}; containerd {safe(pins['containerd_package_version'].split('-')[0])}; Calico {safe(pins['calico_version'])}, VXLAN"],
            ["Gateway API", f"Envoy Gateway {safe(pins['envoy_gateway_version'])}; Gateway API {safe(pins['gateway_api_version'])}; NodePort 30080 / 30443"],
            ["Автоматизация", f"Ansible + Helm {safe(pins['helm_version'])} + Kustomize; make deploy / make verify"],
            ["Метрики и логи", f"kube-prometheus-stack {safe(pins['monitoring_chart_version'])}; Fluentd {safe(pins['fluentd_version'])}; Loki chart {safe(pins['loki_chart_version'])}; Grafana"],
        ]
        y = table(rows, y, [145, usable-145])
        y = section("Поток запроса и наблюдаемость", y)
        x0, x1, x2 = margin, margin+186, width-margin-143
        box("<b>Пользователь</b><br/>HTTP / HTTPS", x0, y)
        box("<b>Envoy Gateway</b><br/>Gateway + HTTPRoute", x1, y)
        box("<b>Nginx v1 / v2</b><br/>Services + Deployments", x2, y)
        arrow(x0+143, y-17, x1-4, y-17)
        arrow(x1+143, y-17, x2-4, y-17)
        box("<b>Prometheus</b><br/>Envoy + Kubernetes", x1, y-65)
        box("<b>Fluentd</b><br/>CRI, stdout / stderr", x2, y-65)
        arrow(x1+71, y-35, x1+71, y-61)
        arrow(x2+71, y-35, x2+71, y-61)
        box("<b>Grafana</b><br/>Метрики + поиск логов", x1, y-127)
        box("<b>Loki</b><br/>TSDB / filesystem", x2, y-127)
        arrow(x1+71, y-100, x1+71, y-123)
        arrow(x2+71, y-100, x2+71, y-123)
        arrow(x2-4, y-144, x1+147, y-144)
        paragraph("UI/API Prometheus, Loki и Grafana: ClusterIP.<br/>Доступ: SSH tunnel / port-forward.<br/>Host ports: см. ограничения.", x0, y-72, 166, small)
        y -= 182
        y = section("Среда по исходным протоколам", y)
        rows = [["<b>Узел / ОС / ресурсы</b>", "<b>Фактический результат</b>"]]
        for vm in data["vms"]:
            rows.append([f"<b>{safe(vm['name'])}</b>: {safe(vm['os'])}<br/>{vm['vcpu']} vCPU / {vm['ram_gib']:g} GiB RAM / {vm['disk_gb']:g} GB disk", f"Проверки: <b>{vm['checks_passed']}/{vm['checks_total']}</b><br/>Bootstrap kubeadm: {yes(vm['clean_deploy'])}<br/>Повторное развертывание: {yes(vm['repeat_deploy'])}"])
        y = table(rows, y, [usable*.52, usable*.48])
        paragraph("Точные версии: " + link("versions.yaml") + ". Протокол: " + link("docs/validation.md") + ".", margin, y, usable, small)
        canvas.showPage()

        y = page(2, "Что реализовано<br/>и как проверить", "Обязательные компоненты и дополнения имеют конфигурацию в репозитории. Проверки завершаются ненулевым кодом при ошибке и сохраняют JSON / JUnit.")
        rows = [["<b>Реализация</b>", "<b>Почему так</b>", "<b>Проверка экспертом</b>"],
            ["<b>Kubernetes / Ubuntu</b><br/>kubeadm, containerd, Calico", "Полный путь от выделенной VM до рабочего кластера", "make preflight; make deploy;<br/>kubectl get nodes,pods -A"],
            ["<b>Gateway + приложение</b><br/>Nginx v1/v2, Gateway, HTTPRoute, NodePort", "Gateway API без зависимости от облачного балансировщика", "make verify: точные HTTP-ответы, актуальные Accepted / Programmed"],
            ["<b>Prometheus + Grafana</b><br/>Helm, PodMonitor, dashboard", "В исходниках исключен служебный HTTP-трафик; на VM A найден drift dashboard", "make verify: свежие scrape и рост request counter; Grafana dashboard"],
            ["<b>Fluentd + Loki</b><br/>CRI fragments, JSON access и текст stderr", "Разделение потоков, поиск по UUID, буфер при временном сбое Loki", "make verify: UUID найден в stdout и stderr; не только kubectl logs"],
            ["<b>Воспроизводимость</b><br/>Ansible, Helm, Kustomize, pin/checksum и локальная сборка", "Нет личного registry; декларативные ресурсы и фиксированные зависимости", "Повторить make deploy / make verify; сверить JSON и dashboard с исходниками"],
            ["<b>Маршрутизация и TLS</b><br/>hostname, /v2 rewrite; локальный CA", "Разные backends и проверка имени сервера без публичного DNS", "curl --resolve и --cacert; verify отвергает чужое имя / CA"],
            ["<b>Canary 90/10</b><br/>Веса backendRefs", "Стандартный механизм Gateway API для постепенного выпуска", "Полный verify: 1000 запросов и статистическая оценка распределения"],
            ["<b>Безопасность</b><br/>Непривилегированный Nginx, probes, limits, NetworkPolicy", "Ограничены права приложения и сетевой доступ; секреты вне Git", "Манифесты + readiness; фактические fault/policy tests в протоколе"],
            ["<b>Сохранность данных</b><br/>Local PV, Retain, Fluentd positions / buffer", "Пересоздание Pod не удаляет данные; ограничен размер очереди", "PVC Bound; проверки восстановления и повторный поиск UUID"],
            ["<b>Документация / CI</b><br/>README, runbook, research; workflow lint / tests", "Эксперт получает команды воспроизведения и честные ограничения", "make lint; docs/validation.md; запуск CI считается только по его отчету"],
        ]
        y = table(rows, y, [usable*.35, usable*.29, usable*.36])
        y = section("Точка входа для проверки", y)
        paragraph("<b>make deploy &nbsp; → &nbsp; make verify</b><br/>HTTP: demo.test:30080; HTTPS: demo.test:30443. Подробные curl-команды, доступ к Grafana и восстановление после ошибок: " + link("README.md") + ", " + link("docs/runbook.md") + ".", margin, y, usable)
        canvas.showPage()

        y = page(3, "Ревью и развитие", "Результаты испытаний, ограничения и направления развития.")
        y = section("Сильная сторона", y)
        y = paragraph("Один воспроизводимый сценарий связывает внешний HTTP-запрос, свежий счетчик Envoy и UUID в двух потоках Loki. Приемка проверяет сам поток данных, а не только готовность установленных компонентов.", margin, y, usable) - 17
        y = section("Самый сложный выбор", y)
        y = paragraph("Вместо Elasticsearch и сложного HA выбран Fluentd + Monolithic Loki с локальными PV: этот вариант укладывается в ресурсы стенда и сохраняет access/error-логи. Компромисс - отсутствие устойчивости к потере VM и необходимость контролировать свободный диск.", margin, y, usable) - 17
        y = section("Факты из исходных протоколов", y)
        rows = [["<b>Прогон</b>", "<b>Canary v1 / v2</b>", "<b>Метрики / логи</b>"]]
        for vm in data["vms"]:
            c = vm["canary"]
            logs = "stdout + stderr" if vm["loki_stdout"] and vm["loki_stderr"] else "есть неподтвержденные потоки"
            rows.append([link(vm["report"], vm["name"]), f"{c['v1']} / {c['v2']} из {c['samples']}", f"Targets UP: {vm['prometheus_targets_up']}<br/>{logs}"])
        y = table(rows, y, [usable*.20, usable*.33, usable*.47])
        unit = data["unit_tests"]
        text = f"Локальные tests: <b>{unit['passed']}/{unit['total']}</b>. "
        if data["fault_tests"]:
            labels = {"PASS": "пройдено", "FAIL": "ошибка", "NOT_RUN": "не выполнялось"}
            text += "Аварийные сценарии: " + "; ".join(link(t["report"], t["name"]) + " - " + labels[t["result"]] for t in data["fault_tests"]) + "."
        else:
            text += "Аварийные сценарии в этой сводке не подтверждены."
        y = paragraph(text, margin, y, usable, small) - 16
        y = section("Дальнейшее развитие с учетом телекома", y)
        future = [
            "<b>Отказоустойчивость:</b> несколько узлов, резерв входного адреса и внешнее хранилище Loki; нужны VM и S3-совместимый сервис.",
            "<b>Качество сервиса:</b> SLO, Alertmanager и нагрузочные профили; нужны получатель уведомлений и отдельный генератор нагрузки.",
            "<b>Телеком-площадки:</b> региональные кластеры, единый обзор и GitOps; нужны межплощадочная сеть и стратегия обновлений.",
        ]
        for line in future:
            y = paragraph("- " + line, margin, y, usable) - 6
        y -= 7
        y = section("Осознанные ограничения", y)
        y = paragraph("Одна VM/local PV не дают HA; PV capacity не является квотой. TLS: локальный CA, внешних уведомлений нет. Независимая проверка: установка на чистую ОС не проверена; на A устаревший dashboard; открыты служебные host ports; на B лишний стартовый рестарт Operator. Подробности: docs/validation.md. CI static PASS; integration не запускался.", margin, y, usable, small) - 10
        revision = safe(data["source_revision"])
        tail = "Источник результатов: " + link("docs/evidence/summary.json") + f". Снимок: {revision}."
        if data.get("repository_url"):
            tail += f' <link href="{safe(data["repository_url"])}" color="#007D80"><u>Опубликованная ветка main</u></link>.'
        else:
            tail += " Ссылка на main прилагается отдельно в Ссылка.txt; пути указаны от корня репозитория."
        paragraph(tail, margin, y, usable, small)
        canvas.showPage()
        canvas.save()
        reader = PdfReader(temporary)
        if len(reader.pages) != 3 or Path(temporary).stat().st_size > 15_000_000:
            raise ValueError("Passport must contain exactly three pages and be <=15 MB")
        for i, p in enumerate(reader.pages, 1):
            if len(p.extract_text().strip()) < 100:
                raise ValueError(f"Page {i} has insufficient extractable text")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", type=Path, default=ROOT / "docs/evidence/summary.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/passport/Паспорт.pdf")
    parser.add_argument("--print-schema", action="store_true")
    args = parser.parse_args()
    if args.print_schema:
        print(json.dumps(SCHEMA, ensure_ascii=False, indent=2))
        return
    if not args.evidence.is_file():
        parser.error(f"Verified evidence file missing: {args.evidence}; use --print-schema")
    data = load_evidence(args.evidence)
    build(data, versions(), args.output.resolve())
    print(f"Created 3-page passport: {args.output.resolve()}")
    print("Required next step: render every page with pdftoppm and visually review before submission.")


if __name__ == "__main__":
    main()
