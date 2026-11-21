from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/evidence')
def page(request:Request):
    return render(request,'evidence')
@router.post('/api/evidence')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=8)
