// chrono.cpp — MCZ weekly scheduler ("cronotermostato") reader (see chrono.h).
// Keeps all chrono logic out of main.cpp; only uses the generic block-read helper
// bleReadRegs() from ble_api.h.
#include "chrono.h"
#include <Arduino.h>
#include "ble_api.h"

static const uint16_t REG_TEMP_ECO     = 0x097C;   // 2428
static const uint16_t REG_TEMP_COMFORT = 0x097D;   // 2429
static const uint16_t REG_PROG_G1_INT1 = 0x097E;   // 2430  (day base = this + (D-1)*48)
static const int      SLOTS_PER_DAY    = 48;       // 30-min slots -> 24 h
static const char*    DOW[7] = {"Mon","Tue","Wed","Thu","Fri","Sat","Sun"};

static void hhmm(int slot, char* out){ int m = slot*30; sprintf(out, "%02d:%02d", m/60, m%60); }

//   high nibble = fan (6=Auto, 1..5=fixed level)
//   low  nibble: bit3=T2 (comfort temp), bit2=T1 (eco temp); bits1..0 = mode
//                (0=Auto, 1=Comfort, 2=Overnight). The chrono offers only these three modes.
static void decodeSlot(uint16_t v, char* out){
  int fan = (v >> 4) & 0x0F, lo = v & 0x0F, mode = lo & 0x03;
  const char* fanS  = (fan==6) ? "Auto" : (fan>=1 && fan<=5) ? nullptr : "?";
  const char* tempS = (lo & 0x08) ? "T2" : (lo & 0x04) ? "T1" : "?";
  const char* modeS = mode==0 ? "Auto" : mode==1 ? "Comfort" : mode==2 ? "Overnight" : "?";
  char fanBuf[4]; if(!fanS){ sprintf(fanBuf,"%d",fan); fanS=fanBuf; }
  sprintf(out, "mode=%s temp=%s fan=%s", modeS, tempS, fanS);
}

// Most frequent value in the day (= the unprogrammed/default slot value).
static uint16_t defaultSlot(const uint16_t* s, int n){
  uint16_t best = s[0]; int bestCnt = 0;
  for(int i=0;i<n;i++){ int c=0; for(int j=0;j<n;j++) if(s[j]==s[i]) c++;
    if(c>bestCnt){ bestCnt=c; best=s[i]; } }
  return best;
}

void chronoPrint(){
  uint16_t hdr[2];
  if(!bleReadRegs(REG_TEMP_ECO, 2, hdr)){ Serial.println("!! chrono: BLE read failed (connected?)"); return; }
  Serial.printf(">> Chrono  temp_eco=%.1f C  temp_comfort=%.1f C  (slots = 30 min)\n",
                hdr[0]/10.0f, hdr[1]/10.0f);
  for(int d=0; d<7; d++){
    uint16_t s[SLOTS_PER_DAY];
    if(!bleReadRegs((uint16_t)(REG_PROG_G1_INT1 + d*SLOTS_PER_DAY), SLOTS_PER_DAY, s)){
      Serial.printf("   %-3s: read failed\n", DOW[d]); continue;
    }
    uint16_t dflt = defaultSlot(s, SLOTS_PER_DAY);
    Serial.printf("   %-3s:", DOW[d]);
    bool any=false; int i=0;
    while(i<SLOTS_PER_DAY){
      if(s[i]==dflt){ i++; continue; }
      int start=i; uint16_t v=s[i];
      while(i<SLOTS_PER_DAY && s[i]==v) i++;
      char a[6], b[6], dec[48]; hhmm(start,a); hhmm(i,b); decodeSlot(v,dec);
      Serial.printf(" %s-%s=%u [%s]", a, b, v, dec); any=true;
    }
    if(!any) Serial.print(" (empty)");
    Serial.println();
  }
  Serial.println("   (fan: 6->Auto or level 1-5; temp T1=eco/T2=comfort; modes Auto/Comfort/Overnight)");
}
