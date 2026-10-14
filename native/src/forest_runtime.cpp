#include "mcp_bridge/forest_runtime.h"
#include "mcp_bridge/handler_helpers.h"
#include <icurvctl.h>
#include <maxscript/foundation/arrays.h>
#include <maxscript/foundation/numbers.h>
#include <maxscript/foundation/functions.h>
#include <cmath>
#include <set>
#include <vector>

namespace {
using namespace HandlerHelpers;
[[noreturn]] void Refuse(const MCHAR* text) { throw RuntimeError(text); }
Animatable* ForestOwner(Value* handle,bool detached) {
    auto* ip=GetCOREInterface();
    if(!ip || GetWindowThreadProcessId(ip->GetMAXHWnd(),nullptr)!=GetCurrentThreadId())
        Refuse(L"MAIN_THREAD_REQUIRED: Forest SDK preparation requires Max's main thread");
    auto* owner=Animatable::GetAnimByHandle((AnimHandle)handle->to_int64());
    if(!owner) Refuse(L"STALE_BINDING: Forest staging handle is unavailable");
    // Verified instance identity: plugin display names differ from the MAXScript constructor.
    const auto classId=owner->ClassID();
    if(owner->SuperClassID()!=GEOMOBJECT_CLASS_ID || classId!=Class_ID(0x79FE41C2,0x1D7B4FB1)) {
        MSTR className,message;
        owner->GetClassName(className,false);
        message.printf(L"NOT_FOREST: expected Forest Pro 79FE41C2:1D7B4FB1, got %08X:%08X superclass %08X name %s",
            classId.PartA(),classId.PartB(),(unsigned int)owner->SuperClassID(),className.data());
        Refuse(message.data());
    }
    if(!detached)return owner;
    std::vector<ReferenceTarget*> pending;
    auto* root=dynamic_cast<ReferenceTarget*>(owner);
    if(!root) Refuse(L"NOT_FOREST: Forest base is not a reference target");
    pending.push_back(root);
    std::set<ReferenceTarget*> seen;
    for(size_t i=0;i<pending.size();++i) {
        if(pending.size()>2048) Refuse(L"REFERENCE_LIMIT: staging ownership traversal exceeded its bound");
        auto* value=pending[i];if(!seen.insert(value).second)continue;
        DependentIterator it(value);
        for(auto* dep=it.Next();dep;dep=it.Next()) {
            if(dep->SuperClassID()==BASENODE_CLASS_ID)
                Refuse(L"STAGING_REQUIRED: SDK preparation only changes an unattached Forest copy");
            if(auto* target=dynamic_cast<ReferenceTarget*>(dep))pending.push_back(target);
        }
    }
    return owner;
}
struct Field { IParamBlock2* block;ParamID id;const ParamDef* def; };
Field FindField(Animatable* owner,const MCHAR* name,int type) {
    for(int i=0;i<owner->NumParamBlocks();++i) {
        auto* pb=owner->GetParamBlock(i);if(!pb||!pb->GetDesc())continue;
        for(int j=0;j<pb->NumParams();++j) {
            auto id=pb->IndextoID(j);auto& def=pb->GetDesc()->GetParamDef(id);
            if(def.int_name&&_wcsicmp(def.int_name,name)==0) {
                if(type>=0&&(int)def.type!=type)Refuse(L"PARAMETER_SCHEMA_UNSUPPORTED: Forest storage type changed");
                return {pb,id,&def};
            }
        }
    }
    Refuse(L"PARAMETER_SCHEMA_UNSUPPORTED: Forest storage field is unavailable");
}
void CheckControlOwnership(Animatable* owner,Animatable* candidate) {
    std::vector<std::pair<Animatable*,int>> pending={{owner,0}};std::set<Animatable*> seen;
    for(size_t i=0;i<pending.size();++i) {
        if(pending.size()>4096)Refuse(L"REFERENCE_LIMIT: CurveControl traversal exceeded its bound");
        auto [value,depth]=pending[i];if(!value||!seen.insert(value).second)continue;
        if(value==candidate)return;
        auto* maker=dynamic_cast<ReferenceMaker*>(value);if(!maker||depth>=4)continue;
        if(maker->NumRefs()>1024)Refuse(L"REFERENCE_LIMIT: too many Forest references");
        for(int j=0;j<maker->NumRefs();++j)pending.push_back({maker->GetReference(j),depth+1});
    }
    Refuse(L"STALE_BINDING: CurveControl is not owned by the staged Forest");
}
Value* PrepareCurves(Value** args,int count) {
    if(count!=4)Refuse(L"BAD_PARAM: mcpForestPrepareCurveSlots expects four arguments");
    auto* owner=ForestOwner(args[0],true);
    auto* candidate=Animatable::GetAnimByHandle((AnimHandle)args[1]->to_int64());
    if(!candidate||candidate->ClassID()!=CURVE_CONTROL_CLASS_ID)
        Refuse(L"NOT_CURVE_CONTROL: staged CurveControl is unavailable");
    CheckControlOwnership(owner,candidate);
    auto* control=static_cast<ICurveCtl*>(candidate);
    const int beforeRows=args[2]->to_int();
    if(!is_array(args[3]))Refuse(L"BAD_PARAM: curve mapping must be an array");
    auto* mapping=static_cast<Array*>(args[3]);
    if(beforeRows<0||beforeRows>100||mapping->size<0||mapping->size>100)
        Refuse(L"BAD_PARAM: Forest parameter counts must be bounded by 100");
    auto types=FindField(owner,L"efpatype",TYPE_INT_TAB);
    auto ids=FindField(owner,L"efpaid",TYPE_STRING_TAB);
    const int rows=types.block->Count(types.id);
    if(rows!=mapping->size||ids.block->Count(ids.id)!=rows)
        Refuse(L"EFFECT_PARAMETER_LIST_MISMATCH: staged curve mapping does not match parameter rows");
    const int oldCount=control->GetNumCurves();
    if(oldCount<0||oldCount>1000)Refuse(L"CURVE_STATE_INVALID: invalid curve count");
    struct Snapshot { int index;int outOfRange;std::vector<CurvePoint> points; };
    std::vector<Snapshot> moved;
    std::vector<int> newCurves;
    const auto time=GetCOREInterface()->GetTime();
    for(int i=0;i<rows;++i) {
        const int source=mapping->data[i]->to_int();
        if(source < -1 || source>=beforeRows)Refuse(L"BAD_PARAM: invalid source parameter index");
        int type=0;Interval valid=FOREVER;
        if(!types.block->GetValue(types.id,time,type,valid,i))Refuse(L"INVALID_READBACK: parameter type is unavailable");
        if(type!=4)continue;
        if(source<0){newCurves.push_back(i);continue;}
        if(source>=oldCount||!control->GetControlCurve(source))
            Refuse(L"CURVE_STATE_INVALID: existing curve parameter has no native curve slot");
        if(source==i)continue;
        auto* curve=control->GetControlCurve(source);const int points=curve->GetNumPts();
        if(points<2||points>256)Refuse(L"CURVE_STATE_INVALID: existing curve point count is unsupported");
        Snapshot item{i,curve->GetOutOfRangeType(),{}};
        for(int j=0;j<points;++j) {
            if(curve->IsAnimated(j))Refuse(L"ANIMATED_CURVE_UNSUPPORTED: cannot reorder animated curve points");
            item.points.push_back(curve->GetPoint(time,j));
        }
        moved.push_back(std::move(item));
    }
    // Snapshot every moved static curve before resizing; preserve unchanged slots.
    control->SetNumCurves(rows,FALSE);
    for(const auto& item:moved) {
        auto* curve=control->GetControlCurve(item.index);if(!curve)Refuse(L"INVALID_READBACK: resized curve slot is unavailable");
        curve->SetNumPts((int)item.points.size());curve->SetOutOfRangeType(item.outOfRange);
        for(int j=0;j<(int)item.points.size();++j) {
            auto point=item.points[j];curve->SetPoint(time,j,&point,FALSE,FALSE);
        }
    }
    for(int index:newCurves) {
        auto* curve=control->GetControlCurve(index);if(!curve)Refuse(L"INVALID_READBACK: new curve slot is unavailable");
        curve->SetNumPts(2);
        for(int j=0;j<2;++j) {
            CurvePoint point;point.p=Point2((float)j,1.0f);point.in=point.out=Point2(0,0);point.flags=CURVEP_CORNER|CURVEP_ENDPOINT;
            curve->SetPoint(time,j,&point,FALSE,FALSE);
        }
    }
    return Integer::intern(control->GetNumCurves());
}
// Writes one static curve's points on a staged (unattached) Forest copy. The slot is the global
// parameter row (verified live: slot index = efpa* row, across effects). Points arrive as
// #(px,py,inx,iny,outx,outy,flags) arrays; every one is validated before any write, and the
// result is read back. Animated curves, unknown flag bits and out-of-range values are refused.
Value* SetCurvePoints(Value** args,int count) {
    if(count!=4)Refuse(L"BAD_PARAM: mcpForestSetCurvePoints expects four arguments");
    auto* owner=ForestOwner(args[0],true);
    auto* candidate=Animatable::GetAnimByHandle((AnimHandle)args[1]->to_int64());
    if(!candidate||candidate->ClassID()!=CURVE_CONTROL_CLASS_ID)
        Refuse(L"NOT_CURVE_CONTROL: staged CurveControl is unavailable");
    CheckControlOwnership(owner,candidate);
    auto* control=static_cast<ICurveCtl*>(candidate);
    const int slot=args[2]->to_int();
    if(!is_array(args[3]))Refuse(L"BAD_PARAM: curve points must be an array");
    auto* input=static_cast<Array*>(args[3]);
    if(input->size<2||input->size>256)Refuse(L"BAD_PARAM: a curve needs 2..256 points");
    auto types=FindField(owner,L"efpatype",TYPE_INT_TAB);
    const int rows=types.block->Count(types.id);
    const auto time=GetCOREInterface()->GetTime();
    if(slot<0||slot>=rows||slot>=control->GetNumCurves())
        Refuse(L"CURVE_STATE_INVALID: curve slot does not match a parameter row");
    int type=0;Interval valid=FOREVER;
    if(!types.block->GetValue(types.id,time,type,valid,slot))Refuse(L"INVALID_READBACK: parameter type is unavailable");
    if(type!=4)Refuse(L"PARAMETER_TYPE_UNSUPPORTED: the parameter row is not a curve");
    auto* curve=control->GetControlCurve(slot);
    if(!curve)Refuse(L"CURVE_STATE_INVALID: curve slot is unavailable");
    for(int j=0;j<curve->GetNumPts();++j)
        if(curve->IsAnimated(j))Refuse(L"ANIMATED_CURVE_UNSUPPORTED: the curve has animated points");
    const Point2 xr=control->GetXRange(),yr=control->GetYRange();
    const int allowed=CURVEP_BEZIER|CURVEP_CORNER|CURVEP_LOCKED_Y|CURVEP_LOCKED_X|CURVEP_ENDPOINT|CURVEP_NO_X_CONSTRAINT;
    constexpr float eps=1e-5f;
    std::vector<CurvePoint> points;
    for(int j=0;j<input->size;++j) {
        if(!is_array(input->data[j]))Refuse(L"BAD_PARAM: each point must be #(px,py,inx,iny,outx,outy,flags)");
        auto* item=static_cast<Array*>(input->data[j]);
        if(item->size!=7)Refuse(L"BAD_PARAM: each point must be #(px,py,inx,iny,outx,outy,flags)");
        float v[6];
        for(int k=0;k<6;++k){v[k]=item->data[k]->to_float();if(!std::isfinite(v[k]))Refuse(L"BAD_PARAM: curve values must be finite");}
        const int flags=item->data[6]->to_int();
        if(flags&~allowed)Refuse(L"BAD_PARAM: unsupported curve point flags");
        if(((flags&CURVEP_BEZIER)!=0)==((flags&CURVEP_CORNER)!=0))Refuse(L"BAD_PARAM: a point is either corner or bezier");
        const bool end=j==0||j==input->size-1;
        if(((flags&CURVEP_ENDPOINT)!=0)!=end)Refuse(L"BAD_PARAM: only the first and last points are endpoints");
        if(v[0]<xr.x-eps||v[0]>xr.y+eps||v[1]<yr.x-eps||v[1]>yr.y+eps)
            Refuse(L"CURVE_VALUE_OUT_OF_RANGE: a point lies outside the curve's X/Y range");
        if(j==0&&std::fabs(v[0]-xr.x)>eps)Refuse(L"BAD_PARAM: the first point must sit at the start of the X range");
        if(j==input->size-1&&std::fabs(v[0]-xr.y)>eps)Refuse(L"BAD_PARAM: the last point must sit at the end of the X range");
        if(!points.empty()&&v[0]<=points.back().p.x+eps)Refuse(L"BAD_PARAM: point X values must increase");
        if(v[2]>eps||v[4]<-eps)Refuse(L"BAD_PARAM: in tangents point left (x<=0), out tangents right (x>=0)");
        CurvePoint p;p.p=Point2(v[0],v[1]);p.in=Point2(v[2],v[3]);p.out=Point2(v[4],v[5]);p.flags=flags;
        points.push_back(p);
    }
    curve->SetNumPts((int)points.size());
    for(int j=0;j<(int)points.size();++j){auto p=points[j];curve->SetPoint(time,j,&p,FALSE,FALSE);}
    if(curve->GetNumPts()!=(int)points.size())Refuse(L"INVALID_READBACK: curve point count did not persist");
    for(int j=0;j<(int)points.size();++j) {
        auto got=curve->GetPoint(time,j);const auto& want=points[j];
        auto off=[&](Point2 a,Point2 b){return std::fabs(a.x-b.x)>eps||std::fabs(a.y-b.y)>eps;};
        if(off(got.p,want.p)||off(got.in,want.in)||off(got.out,want.out)||(got.flags&allowed)!=want.flags)
            Refuse(L"INVALID_READBACK: curve point did not persist as written");
    }
    return Integer::intern((int)points.size());
}
Value* BindFloat(Value** args,int count) {
    if(count!=3)Refuse(L"BAD_PARAM: mcpForestBindFloatController expects three arguments");
    auto* owner=ForestOwner(args[0],true);const int index=args[1]->to_int();
    auto field=FindField(owner,L"efpafloatval",TYPE_FLOAT_TAB);
    if(!(field.def->flags&P_ANIMATABLE)||index<0||index>=field.block->Count(field.id))
        Refuse(L"PARAMETER_SCHEMA_UNSUPPORTED: numeric controller slot is unavailable");
    auto* value=Animatable::GetAnimByHandle((AnimHandle)args[2]->to_int64());
    if(!value||value->SuperClassID()!=CTRL_FLOAT_CLASS_ID)
        Refuse(L"CONTROLLER_TYPE_UNSUPPORTED: numeric driver must be a FloatController");
    auto* controller=static_cast<Control*>(value);
    field.block->SetControllerByID(field.id,index,controller,FALSE);
    if(field.block->GetControllerByID(field.id,index)!=controller)
        Refuse(L"INVALID_READBACK: numeric controller assignment did not persist");
    return Integer64::intern((INT64)Animatable::GetHandleByAnim(controller));
}
Value* ReadFloat(Value** args,int count) {
    if(count!=2)Refuse(L"BAD_PARAM: mcpForestFloatControllerHandle expects two arguments");
    auto* owner=ForestOwner(args[0],false);const int index=args[1]->to_int();
    auto field=FindField(owner,L"efpafloatval",TYPE_FLOAT_TAB);
    if(index<0||index>=field.block->Count(field.id))Refuse(L"BAD_INDEX: numeric controller slot is unavailable");
    auto* controller=field.block->GetControllerByID(field.id,index);
    return Integer64::intern(controller?(INT64)Animatable::GetHandleByAnim(controller):0);
}
Value* ValidateCurves(Value** args,int count) {
    if(count!=2)Refuse(L"BAD_PARAM: mcpForestValidateCurveSlots expects two arguments");
    auto* owner=ForestOwner(args[0],false);
    auto* candidate=Animatable::GetAnimByHandle((AnimHandle)args[1]->to_int64());
    if(!candidate||candidate->ClassID()!=CURVE_CONTROL_CLASS_ID)Refuse(L"CURVE_STATE_INVALID: Forest CurveControl is unavailable");
    CheckControlOwnership(owner,candidate);
    auto* control=static_cast<ICurveCtl*>(candidate);
    auto types=FindField(owner,L"efpatype",TYPE_INT_TAB);
    const int rows=types.block->Count(types.id);
    if(rows<0||rows>100)Refuse(L"CURVE_STATE_INVALID: curve parameter count exceeds the supported bound");
    for(const auto* name:{L"efpaid",L"efpaeffid",L"efpaname",L"efpacurve"}) {
        auto field=FindField(owner,name,-1);
        if(!(field.def->type&TYPE_TAB)||field.block->Count(field.id)!=rows)
            Refuse(L"EFFECT_PARAMETER_LIST_MISMATCH: curve parameter arrays have unequal lengths");
    }
    const int curves=control->GetNumCurves();
    if(curves<0||curves>1000)Refuse(L"CURVE_STATE_INVALID: native curve count is invalid");
    for(int i=0;i<rows;++i) {
        int type=0;Interval valid=FOREVER;
        if(!types.block->GetValue(types.id,GetCOREInterface()->GetTime(),type,valid,i))Refuse(L"INVALID_READBACK: parameter type is unavailable");
        if(type==4&&(i>=curves||!control->GetControlCurve(i)))
            Refuse(L"CURVE_STATE_INVALID: curve parameter has no matching native slot; rebuild its definitions through guarded forest_effects load before reading items");
    }
    return Integer::intern(curves);
}
}
namespace ForestRuntime {
void RegisterPrimitives() {
    // Register after MAXScript has initialized, from the GUP main-thread startup.
    static Primitive prepare(L"mcpForestPrepareCurveSlots",PrepareCurves);
    static Primitive bind(L"mcpForestBindFloatController",BindFloat);
    static Primitive read(L"mcpForestFloatControllerHandle",ReadFloat);
    static Primitive validate(L"mcpForestValidateCurveSlots",ValidateCurves);
    static Primitive points(L"mcpForestSetCurvePoints",SetCurvePoints);
}
}
