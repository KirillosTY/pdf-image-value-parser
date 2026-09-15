# Synthetic fixture evaluation

Real CPU Docling conversion/classification; in-memory Redis storage; no VLM, formatter or SQL run

Status: completed. Elapsed: 11.59 seconds.

| PDF page | Expected panels | Detected labels | Accepted labels |
| --- | --- | --- | --- |
| 2 | bar_chart | bar_chart | bar_chart |
| 3 | line_chart | line_chart | line_chart |
| 4 | pie_chart | pie_chart | pie_chart |
| 5 | scatter_plot | scatter_plot | scatter_plot |
| 6 | box_plot | box_plot | box_plot |
| 7 | heatmap | scatter_plot | scatter_plot |
| 8 | bar_chart, line_chart | line_chart | line_chart |

The five standard single-chart pages (2-6) were correctly classified. Their
classification confidences were 99.88%, 99.90%, 99.99%, 92.75%, and 99.94%,
respectively. These confidence values are model predictions, not measured
numerical extraction accuracy.

Findings:

- The heatmap was classified as scatter_plot at 38.44% confidence. It was still
  accepted. `ExtractionConfig.classification_threshold` defaults to 0.80, but the
  current worker only validates this setting; its selection code does not apply it.
- The mixed-panel page was retained as one crop containing both panels, with
  line_chart confidence 88.19% and bar_chart as the second choice at 11.46%.
  Both plots are present in the saved image; they are not separate image jobs.
- Page 9, the answer key, was detected and accepted as an additional table. The
  eight accepted crops therefore represent seven chart figures plus this table,
  not eight independently detected chart panels. The answer key should be kept
  outside parser inputs in a future blind evaluation.
- Visual inspection of the heatmap and mixed-panel crops showed clipped title
  tops. Their plotted data were visible. Most extracted asset contexts have no
  caption or mentions; this fixture does not exercise realistic figure references.

Numerical accuracy is not tested: no VLM or formatter is configured.
The answer page lists raw box-plot samples, which cannot be recovered individually from the rendered box plot.
Chart-type labels and the answer page make this a diagnostic fixture, not a blind accuracy benchmark.
