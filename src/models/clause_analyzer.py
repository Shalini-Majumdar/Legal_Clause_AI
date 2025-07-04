from typing import Dict, List, Tuple, Optional, Any
import spacy
from transformers import AutoTokenizer, AutoModel
import torch
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import json
import re
import difflib
from Levenshtein import ratio
from sklearn.feature_extraction.text import TfidfVectorizer
from difflib import SequenceMatcher
from transformers import pipeline
import os
import logging
import hashlib


class ClauseAnalyzer:
    def __init__(self, standard_clauses_path: str = "data/raw/standard_clauses.json"):
        """Initialize the ClauseAnalyzer with standard clauses."""
        self.logger = logging.getLogger(__name__)
        self.standard_clauses = self._load_standard_clauses(standard_clauses_path)
        self.nlp = spacy.load("en_core_web_sm")
        
        # Initialize LegalBERT
        self.tokenizer = AutoTokenizer.from_pretrained("nlpaueb/legal-bert-base-uncased")
        self.model = AutoModel.from_pretrained("nlpaueb/legal-bert-base-uncased")
        
        # Pre-compute embeddings for standard clauses
        self.standard_embeddings = self._compute_standard_embeddings()
        
        self.logger.info(f"Loaded {len(self.standard_clauses)} standard clauses.")
        
        self.rewriter = pipeline(
            "text2text-generation",
            model="t5-base",  # or "t5-base", "facebook/bart-large-cnn"
            device=0 if torch.cuda.is_available() else -1
        )
        
        self.llm_cache = {}  # In-memory cache for LLM outputs
        
    def _load_standard_clauses(self, file_path: str):
        with open(file_path, 'r') as f:
            data = json.load(f)
        # If the file is a dict with 'clauses', use that list
        if isinstance(data, dict) and 'clauses' in data:
            return data['clauses']
        # If it's already a list, return as is
        elif isinstance(data, list):
            return data
        else:
            raise ValueError("standard_clauses.json format not recognized")

    def _compute_standard_embeddings(self) -> Dict[str, np.ndarray]:
        """Pre-compute LegalBERT embeddings for all standard clauses."""
        embeddings = {}
        for clause in self.standard_clauses:
            clause_id = clause.get('clause_id') or clause.get('id')
            if not clause_id:
                self.logger.warning(f"Clause missing 'clause_id' or 'id': {clause.get('title', 'No Title')}")
                continue
            text = clause.get('text', '')
            embeddings[clause_id] = self._get_bert_embedding(text)
        return embeddings

    def _get_bert_embedding(self, text: str) -> np.ndarray:
        """Get LegalBERT embedding for a given text."""
        # Tokenize and prepare input
        inputs = self.tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
        
        # Get model output in detail 
        with torch.no_grad():
            outputs = self.model(**inputs)
        
        # Use [CLS] token embedding as sentence representation
        return outputs.last_hidden_state[0][0].numpy()

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts using LegalBERT embeddings."""
        try:
            # Get embeddings
            embedding1 = self._get_bert_embedding(text1)
            embedding2 = self._get_bert_embedding(text2)
            
            # Calculate cosine similarity
            similarity = cosine_similarity([embedding1], [embedding2])[0][0]
            
            # Apply threshold
            return similarity if similarity > 0.5 else 0.0
        except Exception as e:
            print(f"Error calculating similarity: {e}")
            return 0.0

    def _find_matching_clause(self, clause_text: str) -> Tuple[Optional[Dict], float]:
        """Find the best matching standard clause using LegalBERT embeddings."""
        best_match = None
        best_score = 0.0
        
        # Get embedding for input clause
        input_embedding = self._get_bert_embedding(clause_text)
        
        # Compare with all standard clauses
        for clause in self.standard_clauses:
            standard_embedding = self.standard_embeddings[clause['clause_id']]
            score = cosine_similarity([input_embedding], [standard_embedding])[0][0]
            
            if score > best_score:
                best_score = score
                best_match = clause
        
        return best_match, best_score

    def _extract_key_terms(self, text: str) -> List[str]:
        """Extract key terms from text using spaCy."""
        doc = self.nlp(text)
        return [token.text for token in doc if token.pos_ in ['NOUN', 'PROPN'] and not token.is_stop]

    def analyze_document(self, clauses: List[Dict]) -> List[Dict]:
        """Analyze multiple clauses in a document."""
        self.logger.info(f"Analyzing {len(clauses)} clauses in document.")
        results = []
        for i, clause in enumerate(clauses, 1):
            try:
                print(f"Analyzing clause {i}...")
                print(f"Clause structure: {clause.keys()}")
                result = self.analyze_clause(clause)
                results.append(result)
                print(f"Successfully analyzed clause {i}")
            except Exception as e:
                print(f"Error analyzing clause {i}: {str(e)}")
                continue
        return results

    def analyze_clause(self, clause: Dict) -> Dict[str, Any]:
        """Analyze a single clause using LegalBERT."""
        if not isinstance(clause, dict):
            raise ValueError(f"Clause must be a dictionary, got {type(clause)}")
            
        # Get text from either 'text' or 'original_text' key
        clause_text = clause.get('text') or clause.get('original_text')
        if not clause_text:
            raise ValueError("Clause dictionary must contain either 'text' or 'original_text' key")
            
        if not isinstance(clause_text, str):
            raise ValueError(f"Clause text must be a string, got {type(clause_text)}")
            
        clause_title = clause.get('title', '')
        
        # Find matching standard clause
        matching_clause, similarity_score = self._find_matching_clause(clause_text)
        # Ensure similarity_score is a native Python float
        similarity_score = float(similarity_score) if similarity_score is not None else 0.0
        
        if matching_clause and similarity_score > 0.5:
            # Extract key terms
            key_terms = self._extract_key_terms(clause_text)
            
            # Find differences
            differences = self._find_differences(clause_text, matching_clause['text'])
            
            # Generate revision
            suggested_revision = self._generate_revision(matching_clause, clause_text, differences)
            
            return {
                "clause_title": clause_title,
                "original_text": clause_text,
                "analysis_status": "match_found",
                "similarity_score": similarity_score,
                "matching_standard_clause": {
                    "title": matching_clause['title'],
                    "text": matching_clause['text']
                },
                "key_terms": key_terms,
                "differences": differences,
                "suggested_revision": suggested_revision
            }
        else:
            return {
                "clause_title": clause_title,
                "original_text": clause_text,
                "analysis_status": "no_match",
                "similarity_score": similarity_score,
                "key_terms": self._extract_key_terms(clause_text)
            }

    def _find_differences(self, original: str, standard: str) -> List[Dict]:
        """Find differences between original and standard clause at the sentence level."""
        # Split into sentences (very basic, can be improved with nltk.sent_tokenize)
        sentence_split = lambda text: re.split(r'(?<=[.!?]) +', text)
        original_sentences = sentence_split(original)
        standard_sentences = sentence_split(standard)
        diff = difflib.SequenceMatcher(None, original_sentences, standard_sentences)
        differences = []
        for tag, i1, i2, j1, j2 in diff.get_opcodes():
            if tag == 'replace':
                differences.append({
                    'type': 'replace',
                    'original': ' '.join(original_sentences[i1:i2]),
                    'revised': ' '.join(standard_sentences[j1:j2])
                })
            elif tag == 'delete':
                differences.append({
                    'type': 'delete',
                    'original': ' '.join(original_sentences[i1:i2]),
                    'revised': ''
                })
            elif tag == 'insert':
                differences.append({
                    'type': 'insert',
                    'original': '',
                    'revised': ' '.join(standard_sentences[j1:j2])
                })
        return differences

    def _generate_revision(self, standard_clause: Dict, customer_clause: str, differences: List[Dict]) -> str:
        """
        Generate a suggested revision based on the standard clause, but articulated using the customer's language.
        """
        prompt = (
            f"Standard clause:\n{standard_clause['text']}\n\n"
            f"Customer's clause:\n{customer_clause}\n\n"
            "Rewrite the standard clause so that it keeps all the legal requirements and structure of the standard, "
            "but uses the style, terminology, and language of the customer's clause as much as possible. "
            "Output only the revised clause.\n\n"
            "Example:\n"
            "Standard clause: Payment must be made within 30 days.\n"
            "Customer's clause: All invoices are due within 45 days of receipt.\n"
            "Revised clause: Payment must be made within 45 days of receipt.\n"
        )
        result = self.call_llm(prompt)
        return result

    def preprocess_text(self, text: str) -> str:
        """Preprocess text for analysis."""
        if not isinstance(text, str):
            raise ValueError(f"Text must be a string, got {type(text)}")
            
        # Convert to lowercase and remove extra whitespace
        text = text.lower().strip()
        # Remove special characters but keep important punctuation
        text = re.sub(r'[^a-z0-9\s.,;:()]', '', text)
        # Remove extra whitespace
        text = ' '.join(text.split())
        return text
    
    def _identify_clause_type(self, text: str) -> str:
        """Identify the type of clause based on content."""
        text_lower = text.lower()
        
        # Define patterns for different clause types
        patterns = {
            'Definitions': ['definition', 'defined', 'means', 'shall mean'],
            'Payment': ['payment', 'invoice', 'fee', 'cost', 'price', 'amount'],
            'Termination': ['termination', 'terminate', 'end', 'expire', 'cancel'],
            'Confidentiality': ['confidential', 'non-disclosure', 'secret', 'private'],
            'Liability': ['liability', 'liable', 'damages', 'indemnify', 'indemnification'],
            'Intellectual Property': ['intellectual property', 'ip', 'copyright', 'patent', 'trademark'],
            'Force Majeure': ['force majeure', 'act of god', 'beyond control'],
            'Governing Law': ['governing law', 'jurisdiction', 'venue', 'applicable law'],
            'Assignment': ['assignment', 'assign', 'transfer'],
            'Warranty': ['warranty', 'warrant', 'guarantee', 'guaranty'],
            'Service Level': ['service level', 'sla', 'performance', 'uptime'],
            'Data Protection': ['data protection', 'privacy', 'gdpr', 'personal data'],
            'Audit': ['audit', 'inspection', 'review', 'examination']
        }
        
        for clause_type, keywords in patterns.items():
            if any(keyword in text_lower for keyword in keywords):
                return clause_type
        
        return 'General'

    def _categorize_clause(self, text: str) -> str:
        """Categorize a clause based on its content."""
        return self._identify_clause_type(text)

    def call_llm(self, prompt: str) -> str:
        result = self.rewriter(prompt, max_new_tokens=256)
        return result[0]['generated_text']


    def _make_cache_key(self, standard_text, customer_text):
        key = f"{standard_text}|||{customer_text}"
        return hashlib.sha256(key.encode('utf-8')).hexdigest()
    
 