# CITDS 2026 Q&A 庫（英文答稿＝直接照念；括號內為翻頁指引）

Q: Why these five dimensions? / dimension selection / literature basis
A: 【翻到備答 B01 五維度 頁】 The five dimensions answer three questions. Technique and DeceptionSignal describe how a message manipulates. Category and Entity describe what it discusses and who or what is involved. TargetEmotion describes the intended response. Controlled vocabularies keep labels comparable, while Entity remains open-vocabulary.
（若追問）The framework is our integration, but each dimension has a literature basis. Technique follows propaganda taxonomies; Category combines IPTC and Taiwanese media practice; DeceptionSignal follows fake-news reviews; TargetEmotion draws on emotion theory and empirical work; and Entity is open named-entity extraction.

Q: How do the RAG and Baseline prompts differ? / prompt difference / fair comparison
A: 【翻到備答 B02 提示詞 頁】 Both conditions use the same model, temperature, and output format. The baseline receives only the article. RAG also receives the five features, retrieved fake and real cases, and evidence-comparison instructions. Therefore, KG-RAG augmentation is the only manipulated factor; this is not a state-of-the-art comparison.
（若追問）Yes. Both conditions use the same LLM instance, temperature, and output format. Only RAG receives the five features, retrieved cases, and related instructions. The comparison is therefore controlled around the KG-RAG augmentation factor.

Q: How is the extraction prompt designed? / label leakage
A: 【翻到備答 B03 抽取 頁】 The extraction prompt identifies four feature dimensions; it does not classify truthfulness. Three dimensions use controlled label sets, and Entity remains open-vocabulary. The output is JSON. At test time, the fact-check reply is blank and the label field is only a placeholder, so no gold label is supplied.
（若追問）The knowledge graph is built only from the training split, and test articles query it without being written back. During test-time extraction, neither the gold label nor the fact-check reply is provided. Evaluation answers therefore do not enter graph construction or the prompt.

Q: How are retrieval scores formed? / IDF / top-k / threshold
A: 【翻到備答 B04 檢索 頁】 We rank fake and real cases separately using IDF-weighted matching. From each pool, we keep the top K, remove cases below the similarity threshold, and sum the remaining scores. We then apply class-size normalization. Only the retained cases, together with the original article and the five features, enter the RAG prompt.
（若追問）The five features are discrete labels, so exact matching directly shows which patterns overlap and preserves traceability. In the preliminary 1-percent controlled comparison, exact matching achieved the highest accuracy and Macro-F1. Vector and hybrid retrieval remain future directions.

Q: What does each data scale answer? / 1% 10% 100% / why different n
A: 【翻到備答 B05 尺度 頁】 Each scope has a separate role. The 1-percent subset supports tuning and ablation. At 10 percent, Table II reports a three-run mean, while one R1 run supports the paired statistical test. The full 8,527-article set provides the primary three-run result. MyGoPen is a preliminary cross-platform check.
（若追問）The 1-percent point contains only 86 articles and was also used during preliminary tuning. Its result may reflect small-sample uncertainty and a larger proportion of easier, well-covered cases. It is descriptive, not evidence that less data performs better; the 100-percent result is primary.

Q: Does 99.10% mean extraction accuracy? / extraction quality / gold standard
A: 【翻到備答 B06 99.10% 頁】 No. The study has no human-annotated gold standard, so it does not claim semantic extraction accuracy. The audit instead shows 99.10-percent compliance with the controlled vocabularies, valid JSON for all 1,020 responses, and comparable graph-building conditions across runs. Direct evaluation against human labels and inter-annotator agreement remain future work.
（若追問）The current evidence shows controlled output format, vocabulary compliance, and comparable run conditions. It does not prove that every semantic label is correct. Direct evaluation against human annotations and inter-annotator agreement remain future work.

Q: Why not accuracy alone? / class imbalance / trivial baseline
A: 【翻到備答 B07 指標 頁】 Because 82.7 percent of the test set is fake, predicting fake for every article also gives 82.7 percent accuracy but zero specificity. We therefore report fake-news recall, Macro-F1, and the false-positive rate together to show the trade-off between detecting misinformation and incorrectly flagging real news.
（若追問）This operating point is not suitable for automatic decisions. The system is positioned as high-recall triage that prioritizes suspicious articles for fact-checkers. Every flagged item requires human review before action. The paper does not extrapolate whether other parameter settings would reduce FPR.

Q: Are the results statistically significant? / McNemar / why 10%
A: 【翻到備答 B08 統計 頁】 For the 10-percent R1 run of 853 articles, yes. The Wilson 95-percent accuracy intervals do not overlap. The continuity-corrected McNemar test gives chi-square 17.750, with p below 0.001. Cohen's h for fake-news recall is 0.394, a medium effect. No equivalent paired test was reported for the full-set mean.
（若追問）The conference paper reports the paired comparison only for the representative 10-percent R1 run. The 100-percent result is reported as a three-run mean without an equivalent paired test. We therefore state the 10-percent evidence explicitly and do not transfer it to the full-set result.

Q: Why K=4? / retrieval count / other K
A: 【翻到備答 B09 K=4 頁】 K was selected on the 1-percent tuning subset. As K increased, fake-news recall generally rose while specificity fell. Of the tested settings, setting K to four produced the highest accuracy and Macro-F1, so we did not choose the setting with the highest recall. Full-set performance remains the primary evidence.
（若追問）The available evidence supports the selected operating point of K = 4. Whether another K lowers the false-positive rate on the full dataset requires a new operating-point comparison. Such an effect cannot be extrapolated directly from this 1-percent tuning table.

Q: Why this model and exact matching? / why not vector / why Llama Taiwan
A: 【翻到備答 B10 模型 頁】 These are preliminary comparisons on the 1-percent subset. Llama-3-Taiwan-8B-Instruct achieved the highest accuracy and Macro-F1 among the candidate models, and exact matching outperformed vector and hybrid retrieval. Because the features are discrete labels, exact matching also makes the overlap between patterns transparent and traceable.
（若追問）The study uses a locally deployable 8B model to isolate the contribution of KG-RAG augmentation. Preliminary 1B and 3B variants did not reliably follow the extraction protocol. Shared-split comparisons with larger or supervised systems remain future work.
