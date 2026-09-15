"""Compare committed app measurements with independently held fixture values."""
import json
import math
import re
from pathlib import Path


BASE = Path("output/pdf/full_pipeline")
RUN = (BASE / "latest_run.txt").read_text().strip()
FOLDER = BASE / RUN
state = json.loads((FOLDER / "state.json").read_text())
expected = []


def add(page, kind, *, series=None, category=None, row_label=None, statistic=None,
        x=None, y=None, value=None, tolerance=0, printed=True):
    expected.append(dict(page=page, chart_type=kind, series=series, category=category,
                         row_label=row_label, statistic=statistic, x=x, y=y,
                         value=value, tolerance=tolerance, printed=printed))


for series, values in {"North": [120,150,135,180,210], "South": [95,125,145,155,190]}.items():
    for category, value in zip(["Jan","Feb","Mar","Apr","May"], values):
        add(1,"bar_chart",series=series,category=category,value=value)
for series, values in {"Observed":[42,48,57,55,68,76], "Target":[40,45,50,58,65,72]}.items():
    for category, value in zip(range(2020,2026),values):
        add(2,"line_chart",series=series,category=str(category),value=value,
            tolerance=0 if series=="Observed" else 1,printed=series=="Observed")
for category,value in zip(["Product A","Product B","Product C","Other"],[40,30,20,10]):
    add(3,"pie_chart",category=category,value=value)
for series, values in {"Treatment":[2.1,2.8,3.6,4.2,5.1,5.8],"Control":[5.9,5.2,4.8,4.0,3.3,2.7]}.items():
    for x,y in enumerate(values,1):
        add(4,"scatter_plot",series=series,x=x,y=y,tolerance=.15,printed=False)
for series,values in {"Method A":[12,14.5,15,17,18],"Method B":[18,19.5,22,24.5,29],"Method C":[9,11,13,14.5,17]}.items():
    for statistic,value in zip(["lower_whisker","q1","median","q3","upper_whisker"],values):
        add(5,"box_plot",series=series,statistic=statistic,value=value,tolerance=.5,printed=False)
add(5,"box_plot",series="Method A",statistic="outlier",value=21,tolerance=.5,printed=False)
for row,values in {"North":[1,2,3,4],"Central":[2,4,6,8],"South":[3,6,9,12]}.items():
    for category,value in zip(["Q1","Q2","Q3","Q4"],values):
        add(6,"heatmap",category=category,row_label=row,value=value)
for category,value in zip(["Low","Medium","High"],[18,31,24]):
    add(7,"bar_chart",category=category,value=value)
for category,value in enumerate([.22,.28,.31,.37,.41],1):
    add(7,"line_chart",category=str(category),value=value)


def norm(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return {"january":"jan","february":"feb","march":"mar","april":"apr"}.get(text,text)


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):
        return None


def compatible(want,got):
    if want["page"] != got["page"] or want["chart_type"] != got.get("chart_type"):
        return False
    if want["statistic"]:
        statistic={"minimum":"lower_whisker","maximum":"upper_whisker"}.get(got.get("statistic"),got.get("statistic"))
        if statistic!=want["statistic"]:
            return False
    if want["series"] and norm(want["series"]) != norm(got.get("series") or (got.get("category") if want["chart_type"]=="box_plot" else None)):
        return False
    if want["category"] is not None:
        label=norm(got.get("category"))
        if want["page"]==7 and want["chart_type"]=="line_chart":
            label=re.sub(r"^week\s*", "", label)
        if label!=norm(want["category"]):
            return False
    if want["row_label"] and norm(want["row_label"])!=norm(got.get("row_label")):
        return False
    if want["x"] is not None and (numeric(got.get("x")) is None or abs(numeric(got["x"])-want["x"])>.01):
        return False
    return True


page_for={row["image_key"]:row["page_number"] for row in state["images"]}
actual=[dict(row,page=page_for[row["image_key"]]) for row in state["outputs"].get("measurements",[])]
storage_mismatches=[]
for detail in state["details"]:
    source=detail["formatted_data"]["measurements"]
    stored=sorted((r for r in actual if r["image_key"]==detail["image_key"]),key=lambda r:r["ordinal"])
    if len(source)!=len(stored):
        storage_mismatches.append({"image_key":detail["image_key"],"error":"row count mismatch"})
    for ordinal,(a,b) in enumerate(zip(source,stored)):
        for field,value in a.items():
            same=numeric(value)==numeric(b.get(field)) if field in {"x","y","value"} else value==b.get(field)
            if not same:
                storage_mismatches.append({"image_key":detail["image_key"],"ordinal":ordinal,"field":field,"formatted":value,"stored":b.get(field)})
used=set()
matches=[]
for want in expected:
    candidates=[i for i,row in enumerate(actual) if i not in used and compatible(want,row)]
    got=None
    if candidates:
        index=candidates[0]
        used.add(index)
        got=actual[index]
    checks=[]
    for field in ["x","y","value"]:
        if want[field] is None:
            continue
        observed=numeric(got.get(field)) if got else None
        error=abs(observed-want[field]) if observed is not None else None
        tolerance=.01 if field=="x" else want["tolerance"]
        checks.append(dict(field=field,expected=want[field],actual=observed,
                           absolute_error=error,exact=error is not None and error<1e-8,
                           within_tolerance=error is not None and error<=tolerance+1e-8))
    matches.append(dict(expected=want,actual=got,checks=checks))
pages=[]
for page in range(1,8):
    subset=[m for m in matches if m["expected"]["page"]==page]
    checks=[c for m in subset for c in m["checks"]]
    pages.append(dict(input_page=page,original_page=page+1,
        chart_types=sorted({m["expected"]["chart_type"] for m in subset}),
        expected_records=len(subset),matched_records=sum(m["actual"] is not None for m in subset),
        expected_numbers=len(checks),exact_numbers=sum(c["exact"] for c in checks),
        numbers_within_tolerance=sum(c["within_tolerance"] for c in checks),
        extra_records=sum(i not in used and row["page"]==page for i,row in enumerate(actual))))
report={"run_id":RUN,"outcome":state["run"]["outcome"],"stored_documents":len(state["documents"]),
    "formatter_to_sql_mismatches":storage_mismatches,
    "stored_images":len(state["images"]),"stored_measurements":len(actual),"pages":pages,"matches":matches,
    "extra_records":[row for i,row in enumerate(actual) if i not in used],
    "method":"Only committed SQL rows are scored. Match by page/type/series/category/heatmap row/box statistic; scatter by series and x. Month name expansion and 'Week ' prefixes are normalized; unknown or mismatched labels count as missing. Printed values require exact agreement; unlabelled target line tolerance 1, scatter y 0.15 (x 0.01), box statistics 0.5. Box truth uses Matplotlib's default quartiles and 1.5-IQR whiskers: Method A upper whisker is 18 and outlier is 21; hidden raw samples and means are not scored. Units, reading status, panel labels and unmapped observations require separate inspection."}
(FOLDER/"comparison.json").write_text(json.dumps(report,indent=2)+"\n")
lines=["# Full app chart comparison","",f"Run `{RUN}`: {report['outcome']}.","",
       f"Committed: {len(state['documents'])} document, {len(state['images'])} images, {len(actual)} measurements.","",
       "| Original page | Chart | Matched records | Exact numbers | Within tolerance | Extra records |",
       "| --- | --- | --- | --- | --- | --- |"]
for p in pages:
    lines.append(f"| {p['original_page']} | {', '.join(p['chart_types'])} | {p['matched_records']}/{p['expected_records']} | {p['exact_numbers']}/{p['expected_numbers']} | {p['numbers_within_tolerance']}/{p['expected_numbers']} | {p['extra_records']} |")
lines += ["",f"Formatter-to-SQL field mismatches: {len(storage_mismatches)}.","",report["method"],"","The app received the seven-page chart-only PDF through `python -m agent`; it ran real Docling, Ollama VLM and formatter requests, Redis streams/retries, and Postgres transactions. The answer key was excluded before Docling, so it was unavailable as nearby context. Visible chart-type labels remain, making this a diagnostic fixture rather than a general accuracy benchmark."]
(FOLDER/"report.md").write_text("\n".join(lines)+"\n")
print(json.dumps({k:v for k,v in report.items() if k not in {"matches","extra_records","method"}},indent=2))
