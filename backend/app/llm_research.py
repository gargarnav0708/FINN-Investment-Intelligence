"""Optional OpenAI-compatible retrieval adapter; disabled until explicitly configured."""
from __future__ import annotations

import json
import os
import re
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Company, DocumentPage, SourceDocument


def _config():
    base = os.getenv("FINN_LLM_BASE_URL", "").strip().rstrip("/")
    model = os.getenv("FINN_LLM_MODEL", "").strip()
    key = os.getenv("FINN_LLM_API_KEY", "").strip()
    return base, model, key


def retrieve_pages(db: Session, company: Company, question: str, limit: int = 6) -> list[dict]:
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", question.lower()) if token not in {"what", "how", "has", "the", "and", "for", "from", "with", "over", "last"}}
    rows = db.execute(select(DocumentPage, SourceDocument).join(SourceDocument, DocumentPage.document_id == SourceDocument.id).where(
        SourceDocument.company_id == company.id,
        SourceDocument.local_file_path.is_not(None),
    )).all()
    scored=[]
    for page, doc in rows:
        body=page.extracted_text or ""
        low=body.lower()
        score=sum(low.count(token) for token in tokens)
        if score:
            scored.append((score, page.page_number, doc.document_date, page, doc))
    scored.sort(key=lambda x:(x[0],x[2].isoformat() if x[2] else ''),reverse=True)
    return [{"document_id":doc.id,"title":doc.title,"page":page.page_number,"text":(page.extracted_text or "")[:2600]} for _,_,_,page,doc in scored[:limit]]


def answer_with_provider(db: Session, company: Company, question: str) -> dict | None:
    base, model, key = _config()
    if not (base and model):
        return None
    pages=retrieve_pages(db,company,question)
    if not pages:
        return {"status":"abstained","answer":"No matching indexed primary-source pages were retrieved for this question.","citations":[],"formula":None,"mode":"retrieval_llm"}
    evidence="\n\n".join(f"SOURCE document_id={p['document_id']} title={p['title']} page={p['page']}\n{p['text']}" for p in pages)
    payload={"model":model,"temperature":0,"response_format":{"type":"json_object"},"messages":[
        {"role":"system","content":"Answer only from the supplied primary-source excerpts. Return JSON with keys answer and citations. citations must be an array of objects with document_id, page, and quote copied exactly from that page. If evidence is insufficient, say so and return citations []. Do not make recommendations or add outside facts."},
        {"role":"user","content":f"Question: {question}\n\nRetrieved evidence:\n{evidence}"},
    ]}
    headers={"Content-Type":"application/json"}
    if key: headers["Authorization"]=f"Bearer {key}"
    request=Request(f"{base}/chat/completions",data=json.dumps(payload).encode(),headers=headers,method="POST")
    try:
        with urlopen(request,timeout=20) as response:
            result=json.loads(response.read(1_000_000))
        content=result["choices"][0]["message"]["content"]
        parsed=json.loads(content) if isinstance(content,str) else content
    except Exception:
        return {"status":"unavailable","answer":"The configured research model could not return a usable evidence-grounded response.","citations":[],"formula":None,"mode":"retrieval_llm"}
    allowed={(p["document_id"],p["page"]):p for p in pages}
    citations=[]
    for citation in parsed.get("citations",[]):
        try:
            key_tuple=(int(citation["document_id"]),int(citation["page"]))
            source=allowed.get(key_tuple)
            quote=str(citation["quote"]).strip()
            normalized=lambda value:" ".join(str(value).split())
            if source and quote and normalized(quote) in normalized(source["text"]):
                citations.append({"title":source["title"],"page":source["page"],"row":quote,"period":None,"basis":"primary-source excerpt","url":f"/api/documents/{source['document_id']}/file#page={source['page']}"})
        except (KeyError,TypeError,ValueError):
            continue
    answer=str(parsed.get("answer","")).strip()
    if not answer or not citations:
        return {"status":"abstained","answer":"The retrieved material did not support an answer with a verifiable source quote.","citations":[],"formula":None,"mode":"retrieval_llm"}
    return {"status":"available","answer":answer,"citations":citations,"formula":None,"mode":"retrieval_llm"}
