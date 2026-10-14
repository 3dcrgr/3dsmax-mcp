// Optional private live fixture; never part of the MCP tool surface or installer.
#include <max.h>
#include <icurvctl.h>
#include <cstdint>
extern "C" __declspec(dllexport) int SeedScratchCurveControl(std::uint64_t handle) {
    auto* ip=GetCOREInterface();
    if(!ip || GetWindowThreadProcessId(ip->GetMAXHWnd(),nullptr)!=GetCurrentThreadId()) return -1;
    auto* value=Animatable::GetAnimByHandle((AnimHandle)handle);
    if(!value || value->ClassID()!=CURVE_CONTROL_CLASS_ID) return -2;
    auto* control=static_cast<ICurveCtl*>(value);
    if(control->GetNumCurves()!=0) return -3; // Never replace existing curves.
    control->SetNumCurves(2,FALSE);
    control->SetXRange(0.0f,1.0f,FALSE);
    control->SetYRange(-1.0f,2.0f);
    for(int i=0;i<2;++i) {
        auto* curve=control->GetControlCurve(i); if(!curve) return -4;
        curve->SetNumPts(3);
        for(int j=0;j<3;++j) {
            CurvePoint point;
            point.p=Point2(j*0.5f,j==1?0.25f+i:(float)j*0.5f);
            point.in=point.out=Point2(0.0f,0.0f);
            point.flags=CURVEP_CORNER;
            curve->SetPoint(0,j,&point,FALSE,FALSE);
        }
    }
    return control->GetNumCurves();
}
