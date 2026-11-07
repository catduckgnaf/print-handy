import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch
import os
p=os.path.dirname(__file__)
fig,axs=plt.subplots(2,1,figsize=(14,9),gridspec_kw={'height_ratios':[1.65,0.8]},layout='constrained')
# TOP VIEW
ax=axs[0]
ax.set_aspect('equal');ax.set_title('TOP VIEW | 19 in rack • 3 printed parts • late 2014 Mac mini',loc='left',fontsize=15,fontweight='bold')
ax.add_patch(Rectangle((-107,0),214,214,fc='#BBD5ED',ec='#34618A',lw=2,label='Vented cradle'))
ax.add_patch(Rectangle((-241.3,-26),134.3,27,fc='#505965',ec='#222831',label='Left support arm'))
ax.add_patch(Rectangle((107,-26),134.3,27,fc='#505965',ec='#222831',label='Right support arm'))
# two parallel outrigger spines
for x in (-123,108):
 ax.add_patch(Rectangle((x,0),15,161,fc='#505965',ec='none'))
# Mac footprint, rear ports facing top
ax.add_patch(FancyBboxPatch((-98.5,13),197,197,boxstyle='round,pad=0,rounding_size=15',fc='#C5C9CE',ec='#41464C',lw=2,alpha=.86))
ax.text(0,107,'Mac mini\n197 × 197 mm',ha='center',va='center',fontsize=13,color='#282828')
ax.text(0,194,'REAR PORTS',ha='center',va='center',fontsize=9,fontweight='bold')
ax.annotate('',(-232.55,-48),(232.55,-48),arrowprops={'arrowstyle':'<->','color':'#242424','lw':1.2})
ax.text(0,-55,'465.1 mm rack hole centers',ha='center',va='top',fontsize=10)
ax.annotate('',(-241.3,-72),(241.3,-72),arrowprops={'arrowstyle':'<->','color':'#242424','lw':1.2})
ax.text(0,-77,'482.6 mm outside width (19 in)',ha='center',va='top',fontsize=10)
for x in (-232.55,232.55):
 ax.plot([x],[ -16],'o',ms=6,c='white',mec='black')
ax.annotate('',(124,0),(124,214),arrowprops={'arrowstyle':'<->','lw':1.2})
ax.text(130,115,'214 mm tray depth',rotation=90,va='center',fontsize=9)
ax.set(xlim=(-265,265),ylim=(-98,242));ax.axis('off')

# FRONT VIEW x vs z
ax=axs[1]
ax.set_aspect('equal');ax.set_title('FRONT VIEW | 2U height | M4 captured nuts and M6 rack screw slots',loc='left',fontsize=14,fontweight='bold')
for x in (-241.3,220):
 ax.add_patch(Rectangle((x,0),21.3,88.5,fc='#505965',ec='#222831',lw=2))
for x in (-220,107):
 ax.add_patch(Rectangle((x,18),113,57,fc='#505965',ec='#222831'))
ax.add_patch(Rectangle((-107,10),214,10,fc='#BBD5ED',ec='#34618A',lw=1.5))
ax.add_patch(Rectangle((-98.5,20),197,36,fc='#C5C9CE',ec='#41464C',lw=1.5))
for x in (-232.55,232.55):
 for z in (6.35,22.225,66.675,82.55):
  ax.plot(x,z,'o',ms=5,mfc='white',mec='#141414')
ax.text(0,38,'Mac mini | 36 mm tall',ha='center',va='center',fontsize=10)
ax.annotate('',(255,0),(255,88.5),arrowprops={'arrowstyle':'<->'})
ax.text(259,45,'2U = 88.9 mm',rotation=90,va='center')
ax.set(xlim=(-270,278),ylim=(-12,106));ax.axis('off')
fig.savefig(os.path.join(p,'DIMENSIONED_OVERVIEW.png'),dpi=155,facecolor='white')
print('dimension drawing done')
